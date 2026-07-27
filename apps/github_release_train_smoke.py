"""Deterministic state-machine coverage for the GitHub Release Train."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shlex
import sys
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_train import (  # noqa: E402
    AWAITING_AGENT_LABEL,
    AWAITING_UI_LABEL,
    BLOCKED_LABEL,
    Candidate,
    complete_production_mutation_release,
    DONE_LABEL,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    LOOP_ACK_PREFIX,
    LOOP_TASK_LABEL,
    NEEDS_RESUME_LABEL,
    PRODUCTION_LABEL,
    PRODUCTION_MUTATION_LABEL,
    READY_LABEL,
    REPO_ONLY_LABEL,
    RUNNING_LABEL,
    STANDARD_TASK_LABEL,
    SUPERSEDED_LABEL,
    ReleaseBlocked,
    ReleaseClassificationBlocked,
    ReleaseTrainError,
    accept_loop_ui,
    acknowledge_loop_agent,
    complete_standard_release,
    correct_loop_identity_to_new,
    enqueue_loop_new,
    enqueue_loop_recovery,
    handle_loop_comment,
    loop_ack_label,
    loop_root_label,
    loop_root_from_labels,
    loop_registration_kind,
    mark_loop_awaiting_ui,
    mark_classification_blocked,
    merge_candidate,
    prepare_candidate,
    parse_production_mutation_terminalization_command,
    production_mutation_terminal_state_proven,
    production_mutation_terminalization_preflight,
    request_loop_agent,
    resume_halted_release,
    resume_loop_owner,
    retry_blocked_release,
    require_deploy_environment,
    scope_from_labels,
    select_candidate,
    set_release_state,
    task_class_from_labels,
    transition_label_set,
    upsert_status_comment,
)
from apps.github_release_train_wait import (  # noqa: E402
    EXIT_AWAIT_PHASE_CAPABILITY,
    EXIT_AWAITING_UI,
    EXIT_BLOCKED,
    EXIT_CONTINUE_SAFE_PHASES,
    EXIT_CONTINUE_WAITING,
    EXIT_OWN_ACTION,
    EXIT_RESUMED,
    EXIT_TERMINAL_FAILURE,
    build_parser,
    evaluate_release,
    goal_disposition,
    local_playwright_preflight,
    shepherd_release,
    wait_for_release,
)
from apps.github_release_train_spec import (  # noqa: E402
    ACTIVE_PRIMARY_LABELS,
    ACTIVE_STATE_LABELS,
    CANONICAL_MONITOR_URL,
    CANONICAL_PRODUCTION_TARGET_ID,
    CRITICAL_TRANSITIONS,
    EXPLICIT_TASK_PROMPTS,
    ExecutionContour,
    GoalDisposition,
    GoalCapability,
    GoalPhase,
    GoalPhaseContext,
    MONITORED_RELEASE_LABELS,
    PRIMARY_STATE_LABELS,
    TERMINAL_LABELS,
    TERMINAL_FORBIDDEN_INHERITANCE,
    TRANSITION_MATRIX,
    PRODUCTION_MUTATION_RUNNER_REQUIREMENTS,
    PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
    TaskClass,
    TaskContinuity,
    TaskIntent,
    UiRuntime,
    ContinuityIntent,
    classify_continuity,
    classify_task,
    explicit_task_class,
    github_closure_required,
    mcp_capability_sufficient,
    order_goal_phases,
    phase_goal_decision,
    production_evidence_route,
    production_mutation_runner_contract,
    select_ui_runtime,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
EVIDENCE = "sha256:" + "d" * 64


class FakeApi:
    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comparisons: list[dict[str, Any]] = [{"behind_by": 0}]
        self.checks: list[dict[str, Any]] = []
        self.updated: list[tuple[int, str]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.ensured_labels: list[str] = []
        self.replaced_labels: list[tuple[int, set[str]]] = []
        self.comments: list[tuple[int, str]] = []
        self.comment_ids: list[int] = []
        self.comment_metadata: dict[int, dict[str, Any]] = {}
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.merges: list[tuple[int, str]] = []
        self.deleted: list[str] = []

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self.ensured_labels.append(name)

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]:
        return [
            pull
            for pull in self.pulls.values()
            if label in _labels(pull) and (state == "all" or pull.get("state") == state)
        ]

    def list_issue_events(self, number: int) -> list[dict[str, Any]]:
        return list(self.events.get(number, []))

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return [
            {
                "id": comment_id,
                "body": body,
                **self.comment_metadata.get(comment_id, {}),
            }
            for comment_id, (comment_number, body) in zip(self.comment_ids, self.comments)
            if comment_number == number
        ]

    def get_pull(self, number: int) -> dict[str, Any]:
        return self.pulls[number]

    def compare(self, base: str, head: str) -> dict[str, Any]:
        if len(self.comparisons) > 1:
            return self.comparisons.pop(0)
        return self.comparisons[0]

    def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.updated.append((number, expected_head_sha))
        self.pulls[number]["head"]["sha"] = SHA_B

    def dispatch_workflow(self, workflow: str, ref: str) -> None:
        self.dispatched.append((workflow, ref))
        if workflow == "baseline-ci.yml":
            next_id = max((int(item.get("id") or 0) for item in self.checks), default=0) + 1
            self.checks.append(
                {"id": next_id, "name": "baseline", "status": "completed", "conclusion": "success"}
            )

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        return list(self.checks)

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]:
        self.merges.append((number, expected_head_sha))
        merge_sha = f"{number:040x}"
        pull = self.pulls[number]
        pull["state"] = "closed"
        pull["merged"] = True
        pull["merge_commit_sha"] = merge_sha
        return {"merged": True, "sha": merge_sha}

    def add_labels(self, number: int, labels: Iterable[str]) -> None:
        current = _labels(self.pulls[number])
        additions = {str(label) for label in labels} - current
        current.update(additions)
        _set_labels(self.pulls[number], current)
        for label in sorted(additions):
            self.events.setdefault(number, []).append(
                {
                    "event": "labeled",
                    "label": {"name": label},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def set_labels(self, number: int, labels: Iterable[str]) -> None:
        before = _labels(self.pulls[number])
        after = {str(label) for label in labels}
        self.replaced_labels.append((number, set(after)))
        _set_labels(self.pulls[number], after)
        for label in sorted(after - before):
            self.events.setdefault(number, []).append(
                {
                    "event": "labeled",
                    "label": {"name": label},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    def remove_label(self, number: int, label: str) -> None:
        current = _labels(self.pulls[number])
        current.discard(label)
        _set_labels(self.pulls[number], current)

    def add_comment(self, number: int, body: str) -> None:
        comment_id = max(self.comment_ids, default=0) + 1
        self.comments.append((number, body))
        self.comment_ids.append(comment_id)
        self.comment_metadata[comment_id] = {
            "user": {"login": "github-actions[bot]"},
            "author_association": "CONTRIBUTOR",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def add_external_comment(
        self,
        number: int,
        body: str,
        *,
        actor: str = "orenvlad-ai",
        association: str = "OWNER",
        created_at: str,
    ) -> int:
        comment_id = max(self.comment_ids, default=0) + 1
        self.comments.append((number, body))
        self.comment_ids.append(comment_id)
        self.comment_metadata[comment_id] = {
            "user": {"login": actor},
            "author_association": association,
            "created_at": created_at,
        }
        return comment_id

    def update_comment(self, comment_id: int, body: str) -> None:
        index = self.comment_ids.index(comment_id)
        number, _ = self.comments[index]
        self.comments[index] = (number, body)

    def delete_comment(self, comment_id: int) -> None:
        index = self.comment_ids.index(comment_id)
        self.comment_ids.pop(index)
        self.comments.pop(index)
        self.comment_metadata.pop(comment_id, None)

    def close_pull(self, number: int) -> None:
        self.pulls[number]["state"] = "closed"

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)


def _labels(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in payload.get("labels") or []
    }


def _set_labels(payload: dict[str, Any], labels: Iterable[str]) -> None:
    payload["labels"] = [{"name": label} for label in sorted(set(labels))]


def _body_fingerprint(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _pull(
    number: int,
    *,
    labels: list[str],
    created_at: str,
    sha: str = SHA_A,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "created_at": created_at,
        "labels": [{"name": label} for label in labels],
        "pull_request": {"url": f"https://example.invalid/{number}"},
        "base": {"ref": "main"},
        "head": {
            "sha": sha,
            "ref": f"feature/{number}",
            "repo": {"full_name": "orenvlad-ai/wb-core"},
        },
    }


def _prepare(api: FakeApi, number: int) -> Candidate:
    labels = _labels(api.pulls[number])
    if LOOP_TASK_LABEL in labels:
        if not any(
            item.get("name") == "baseline" and item.get("conclusion") == "success"
            for item in api.checks
        ):
            api.checks.append(
                {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
            )
        root = loop_root_from_labels(labels)
        head_sha = str(api.pulls[number]["head"]["sha"])
        new_proven = any(
            f"wb-core-loop-new-root-proof head={head_sha} pr={number} root={number}" in body
            for comment_number, body in api.comments
            if comment_number == number
        )
        if root is None or (root == number and not new_proven):
            enqueue_loop_new(
                api,
                number,
                head_sha,
                actor="codex",
                association="OWNER",
            )
        elif root < number and not any(
            "wb-core-loop-recovery-proof" in body
            for comment_number, body in api.comments
            if comment_number == number
        ):
            gates = api.list_issues_by_label(AWAITING_UI_LABEL, state="all")
            assert len(gates) == 1
            enqueue_loop_recovery(
                api,
                number,
                head_sha,
                gate_pr=int(gates[0]["number"]),
                expected_root=root,
                actor="codex",
                association="OWNER",
            )
    return prepare_candidate(
        api,
        "orenvlad-ai/wb-core",
        number,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )


def _add_new_root_proof(api: FakeApi, number: int) -> None:
    pull = api.pulls[number]
    api.add_labels(number, [loop_root_label(number)])
    api.add_comment(
        number,
        "<!-- wb-core-loop-new-root-proof "
        f"head={pull['head']['sha']} pr={number} root={number} -->",
    )


def _assert_label_and_input_validation() -> None:
    try:
        transition_label_set(
            {READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL, BLOCKED_LABEL},
            RUNNING_LABEL,
        )
    except ReleaseBlocked:
        pass
    else:
        raise AssertionError("conflicting primary states must fail closed")
    running = transition_label_set(
        {READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL},
        RUNNING_LABEL,
    )
    assert running == {READY_LABEL, RUNNING_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL}
    assert transition_label_set(running, DONE_LABEL) == {
        DONE_LABEL,
        STANDARD_TASK_LABEL,
        REPO_ONLY_LABEL,
    }
    assert task_class_from_labels({STANDARD_TASK_LABEL}) == STANDARD_TASK_LABEL
    assert task_class_from_labels({LOOP_TASK_LABEL}) == LOOP_TASK_LABEL
    for labels in (set(), {"task:unknown"}, {STANDARD_TASK_LABEL, LOOP_TASK_LABEL}):
        try:
            task_class_from_labels(labels)
        except ReleaseBlocked:
            pass
        else:
            raise AssertionError(f"invalid task class must block: {labels}")
    for labels in (set(), {REPO_ONLY_LABEL, LIVE_RUNTIME_LABEL}):
        try:
            scope_from_labels(labels)
        except ReleaseBlocked:
            pass
        else:
            raise AssertionError(f"invalid scope must block: {labels}")


def _assert_standard_repo_only_and_live() -> None:
    api = FakeApi()
    repo = _pull(
        1,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-17T01:00:00Z",
    )
    live = _pull(
        2,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T02:00:00Z",
        sha=SHA_B,
    )
    api.pulls = {1: repo, 2: live}
    assert select_candidate(api)["pr_number"] == 1
    candidate = _prepare(api, 1)
    assert candidate.task_class == STANDARD_TASK_LABEL
    assert candidate.agent_acknowledged is False
    result = merge_candidate(api, candidate)
    assert not result.skip_release
    set_release_state(api, 1, DONE_LABEL)
    assert evaluate_release(_labels(repo))["action"] == "success"

    candidate = _prepare(api, 2)
    assert candidate.deploy_required and not candidate.agent_acknowledged
    result = merge_candidate(api, candidate)
    assert not result.skip_release
    set_release_state(api, 2, PRODUCTION_LABEL)
    assert evaluate_release(_labels(live))["action"] == "success"
    repeated = merge_candidate(api, candidate)
    assert repeated.skip_release
    assert len(api.merges) == 2


def _assert_loop_handshake_and_gate() -> tuple[FakeApi, dict[str, Any]]:
    api = FakeApi()
    loop = _pull(
        10,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T03:00:00Z",
    )
    unrelated = _pull(
        11,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-17T04:00:00Z",
        sha=SHA_C,
    )
    api.pulls = {10: loop, 11: unrelated}
    first = _prepare(api, 10)
    assert first.task_class == LOOP_TASK_LABEL and not first.agent_acknowledged
    request_loop_agent(api, 10, SHA_A)
    assert AWAITING_AGENT_LABEL in _labels(loop)
    waiting = select_candidate(
        api,
        now=datetime(2026, 7, 17, 5, 32, tzinfo=timezone.utc).timestamp(),
    )
    assert waiting["status"] == "awaiting-agent" and not waiting["found"]
    try:
        acknowledge_loop_agent(api, 10, SHA_B, actor="codex", association="OWNER")
    except ReleaseBlocked as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale acknowledgement must be rejected")
    assert acknowledge_loop_agent(api, 10, SHA_A, actor="codex", association="OWNER") == "acknowledged"
    assert loop_ack_label(SHA_A) in _labels(loop)
    assert (
        acknowledge_loop_agent(api, 10, SHA_A, actor="codex", association="OWNER")
        == "already-acknowledged"
    )
    second = _prepare(api, 10)
    assert second.agent_acknowledged
    result = merge_candidate(api, second)
    assert loop_ack_label(SHA_A) not in _labels(loop)
    assert api.merges == [(10, SHA_A)]
    root, status = mark_loop_awaiting_ui(api, 10, result.merge_sha)
    assert (root, status) == (10, "awaiting-ui")
    assert {AWAITING_UI_LABEL, loop_root_label(10)} <= _labels(loop)
    assert evaluate_release(_labels(loop))["action"] == "awaiting-ui"
    assert select_candidate(api)["status"] == "awaiting-ui"
    return api, loop


def _assert_recovery_transfer_and_acceptance(api: FakeApi, root: dict[str, Any]) -> None:
    unrelated = api.pulls[11]
    recovery = _pull(
        12,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(10)],
        created_at="2026-07-17T05:00:00Z",
        sha=SHA_B,
    )
    unlinked = _pull(
        13,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T04:30:00Z",
        sha=SHA_C,
    )
    api.pulls.update({12: recovery, 13: unlinked})
    selected = select_candidate(api)
    assert selected["pr_number"] == 12
    first = _prepare(api, 12)
    assert not first.agent_acknowledged and first.loop_root == 10
    request_loop_agent(api, 12, SHA_B)
    acknowledge_loop_agent(api, 12, SHA_B, actor="codex", association="OWNER")
    second = _prepare(api, 12)
    assert second.agent_acknowledged
    result = merge_candidate(api, second)
    mark_loop_awaiting_ui(api, 12, result.merge_sha)
    assert AWAITING_UI_LABEL not in _labels(root)
    assert AWAITING_UI_LABEL in _labels(recovery)
    assert loop_root_label(10) in _labels(recovery)
    api.add_labels(10, [AWAITING_UI_LABEL])
    assert mark_loop_awaiting_ui(api, 12, result.merge_sha)[1] == "already-awaiting-ui"
    assert AWAITING_UI_LABEL not in _labels(root)
    assert select_candidate(api)["status"] == "awaiting-ui"

    dispatched_before = len(api.dispatched)
    assert accept_loop_ui(
        api,
        12,
        actor="codex",
        association="OWNER",
        deployed_sha=result.merge_sha,
        evidence=EVIDENCE,
    ) == "accepted"
    assert PRODUCTION_LABEL not in _labels(root)
    assert PRODUCTION_LABEL in _labels(recovery)
    assert select_candidate(api)["pr_number"] == 11
    assert accept_loop_ui(
        api,
        12,
        actor="codex",
        association="OWNER",
        deployed_sha=result.merge_sha,
        evidence=EVIDENCE,
    ) == "already-accepted"
    assert len(api.dispatched) == dispatched_before + 2
    assert PRODUCTION_LABEL not in _labels(root) and PRODUCTION_LABEL in _labels(recovery)
    assert READY_LABEL in _labels(unrelated)


def _assert_foreign_gate_waiting_and_queue_progress() -> None:
    api = FakeApi()
    loop_a = _pull(
        50,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(50)],
        created_at="2026-07-17T05:30:00Z",
    )
    loop_a["state"] = "closed"
    loop_a["merged"] = True
    loop_a["merge_commit_sha"] = SHA_A
    loop_b = _pull(
        51,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T05:31:00Z",
        sha=SHA_B,
    )
    api.pulls = {50: loop_a, 51: loop_b}
    _add_new_root_proof(api, 50)
    api.add_comment(50, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=50 root=50 -->")

    waiting = select_candidate(
        api,
        now=datetime(2026, 7, 17, 5, 32, tzinfo=timezone.utc).timestamp(),
    )
    assert waiting["status"] == "awaiting-ui" and not waiting["found"]
    decision = evaluate_release(
        _labels(loop_b),
        pr_number=51,
        queue={"status": "awaiting-ui", "pr_number": 50},
    )
    assert decision["action"] == "wait-foreign-gate"
    assert "blocked" not in decision["reason"]
    linked_recovery = _labels(loop_b) | {loop_root_label(50)}
    assert evaluate_release(
        linked_recovery,
        pr_number=51,
        queue={"status": "awaiting-ui", "pr_number": 50, "loop_root": 50},
    )["action"] == "wait"

    set_release_state(api, 50, PRODUCTION_LABEL)
    assert select_candidate(api)["pr_number"] == 51
    candidate = _prepare(api, 51)
    request_loop_agent(api, 51, candidate.head_sha)
    assert AWAITING_AGENT_LABEL in _labels(loop_b)


def _assert_lost_owner_resume_lifecycle() -> None:
    api = FakeApi()
    loop = _pull(
        60,
        labels=[AWAITING_AGENT_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T06:00:00Z",
    )
    queued = _pull(
        61,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-17T06:01:00Z",
        sha=SHA_B,
    )
    api.pulls = {60: loop, 61: queued}
    _add_new_root_proof(api, 60)
    api.events[60] = [
        {
            "event": "labeled",
            "label": {"name": AWAITING_AGENT_LABEL},
            "created_at": "2026-07-17T06:00:00Z",
        }
    ]
    observed_at = datetime(2026, 7, 17, 6, 31, tzinfo=timezone.utc).timestamp()
    first = select_candidate(api, needs_resume_after_seconds=1800, now=observed_at)
    assert first["status"] == "awaiting-agent" and first["needs_resume"]
    assert NEEDS_RESUME_LABEL in _labels(loop)
    status_comments = [body for _, body in api.comments if "wb-core-release-status" in body]
    assert len(status_comments) == 1
    assert "python3 apps/github_release_train_wait.py 60" in status_comments[0]
    assert SHA_A in status_comments[0]
    second = select_candidate(api, needs_resume_after_seconds=1800, now=observed_at + 300)
    assert second["status"] == "awaiting-agent" and second["needs_resume"]
    assert len([body for _, body in api.comments if "wb-core-release-status" in body]) == 1
    assert READY_LABEL in _labels(queued)

    try:
        acknowledge_loop_agent(api, 60, SHA_A, actor="codex", association="OWNER")
    except ReleaseBlocked as exc:
        assert "resume" in str(exc)
    else:
        raise AssertionError("stale owner overlay must require explicit resume before ack")
    resume_loop_owner(
        api,
        60,
        SHA_A,
        60,
        actor="codex",
        association="OWNER",
    )
    acknowledge_loop_agent(api, 60, SHA_A, actor="codex", association="OWNER")
    assert NEEDS_RESUME_LABEL not in _labels(loop)
    assert READY_LABEL in _labels(loop)
    resumed = _prepare(api, 60)
    assert resumed.agent_acknowledged
    assert not merge_candidate(api, resumed).skip_release


def _assert_superseded_normalization_is_root_bounded() -> None:
    api = FakeApi()
    root = _pull(
        70,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(70)],
        created_at="2026-07-17T07:00:00Z",
    )
    root["state"] = "closed"
    root["merged"] = True
    root["merge_commit_sha"] = SHA_A
    failed = _pull(
        71,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(70)],
        created_at="2026-07-17T07:01:00Z",
    )
    other_root = _pull(
        72,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(700)],
        created_at="2026-07-17T07:02:00Z",
    )
    accepted = _pull(
        73,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(70)],
        created_at="2026-07-17T07:03:00Z",
    )
    accepted["state"] = "closed"
    accepted["merged"] = True
    api.pulls = {70: root, 71: failed, 72: other_root, 73: accepted}

    _add_new_root_proof(api, 70)
    api.add_comment(70, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=70 root=70 -->")
    api.add_comment(
        71,
        f"<!-- wb-core-loop-recovery-proof gate=70 head={SHA_A} pr=71 root=70 -->",
    )
    api.add_comment(
        73,
        f"<!-- wb-core-loop-recovery-proof gate=70 head={SHA_A} pr=73 root=70 -->",
    )
    accepted["merge_commit_sha"] = SHA_C
    marker = (
        "<!-- wb-core-loop-deploy-proof merge=" + SHA_C + " pr=73 root=70 -->"
    )
    api.add_comment(73, marker)
    assert accept_loop_ui(
        api,
        73,
        actor="codex",
        association="OWNER",
        deployed_sha=SHA_C,
        evidence=EVIDENCE,
    ) == "accepted"
    assert PRODUCTION_LABEL not in _labels(root)
    assert PRODUCTION_LABEL in _labels(accepted)
    assert SUPERSEDED_LABEL in _labels(failed)
    assert BLOCKED_LABEL not in _labels(failed)
    assert loop_root_label(70) in _labels(failed)
    assert failed["state"] == "closed"
    assert BLOCKED_LABEL in _labels(other_root)
    assert SUPERSEDED_LABEL not in _labels(other_root)

    unsafe_api = FakeApi()
    unsafe_root = _pull(
        74,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(74)],
        created_at="2026-07-17T07:04:00Z",
    )
    unsafe_root.update(state="closed", merged=True, merge_commit_sha=SHA_A)
    manual_member = _pull(
        75,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(74)],
        created_at="2026-07-17T07:05:00Z",
    )
    unsafe_accepted = _pull(
        76,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(74)],
        created_at="2026-07-17T07:06:00Z",
    )
    unsafe_accepted.update(state="closed", merged=True, merge_commit_sha=SHA_C)
    unsafe_api.pulls = {74: unsafe_root, 75: manual_member, 76: unsafe_accepted}
    _add_new_root_proof(unsafe_api, 74)
    unsafe_api.add_comment(
        74, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=74 root=74 -->"
    )
    unsafe_api.add_comment(
        76,
        f"<!-- wb-core-loop-recovery-proof gate=74 head={SHA_A} pr=76 root=74 -->",
    )
    unsafe_api.add_comment(
        76, f"<!-- wb-core-loop-deploy-proof merge={SHA_C} pr=76 root=74 -->"
    )
    labels_before = {number: set(_labels(pull)) for number, pull in unsafe_api.pulls.items()}
    states_before = {number: str(pull["state"]) for number, pull in unsafe_api.pulls.items()}
    try:
        accept_loop_ui(
            unsafe_api,
            76,
            actor="codex",
            association="OWNER",
            deployed_sha=SHA_C,
            evidence=EVIDENCE,
        )
    except ReleaseTrainError as exc:
        assert "ambiguous LOOP chain membership for PR #75" in str(exc)
    else:
        raise AssertionError("manual same-root identity must block terminal cleanup")
    assert {number: set(_labels(pull)) for number, pull in unsafe_api.pulls.items()} == labels_before
    assert {number: str(pull["state"]) for number, pull in unsafe_api.pulls.items()} == states_before
    assert not any(
        "UI evidence accepted" in body
        for comment_number, body in unsafe_api.comments
        if comment_number == 76
    )


def _assert_blocked_halted_and_production_mutation() -> None:
    api = FakeApi()
    mutation = _pull(
        20,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL],
        created_at="2026-07-17T06:00:00Z",
    )
    api.pulls[20] = mutation
    try:
        _prepare(api, 20)
    except ReleaseBlocked as exc:
        assert "human gate" in str(exc)
    else:
        raise AssertionError("production mutation must never auto-release")
    set_release_state(api, 20, BLOCKED_LABEL)
    assert evaluate_release(_labels(mutation))["action"] == "blocked"
    try:
        require_deploy_environment({})
    except ReleaseBlocked as exc:
        assert "production secrets" in str(exc)
    else:
        raise AssertionError("missing deploy secrets must block before merge")

    halted = _pull(
        21,
        labels=[HALTED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T07:00:00Z",
    )
    halted["state"] = "closed"
    halted["merged"] = True
    api.pulls[21] = halted
    result = select_candidate(api)
    assert result == {"status": "halted", "found": False, "halted_pr_number": 21}
    assert evaluate_release(_labels(halted))["action"] == "blocked"

    fixed = _pull(
        22,
        labels=[BLOCKED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-17T07:01:00Z",
        sha=SHA_C,
    )
    api.pulls[22] = fixed
    api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    try:
        retry_blocked_release(
            api,
            22,
            expected_head_sha=SHA_B,
            check_name="baseline",
        )
    except ReleaseBlocked as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("blocked retry must bind the exact current head")
    assert retry_blocked_release(
        api,
        22,
        expected_head_sha=SHA_C,
        check_name="baseline",
    ) == READY_LABEL
    assert READY_LABEL in _labels(fixed) and BLOCKED_LABEL not in _labels(fixed)
    assert api.replaced_labels[-1] == (22, _labels(fixed))


def _production_mutation_terminal_fixture(
    *,
    number: int = 120,
) -> tuple[FakeApi, str, dict[str, Any]]:
    api = FakeApi()
    pull = _pull(
        number,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL],
        created_at="2026-07-21T01:00:00Z",
        sha=SHA_A,
    )
    api.pulls[number] = pull
    api.checks = [
        {
            "id": number,
            "name": "baseline",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    try:
        prepare_candidate(
            api,
            "orenvlad-ai/wb-core",
            number,
            check_name="baseline",
            timeout_seconds=1,
            poll_seconds=0,
        )
    except ReleaseBlocked as exc:
        assert "human gate" in str(exc)
    else:
        raise AssertionError("production mutation fixture auto-released unexpectedly")
    set_release_state(api, number, BLOCKED_LABEL)
    gate_body = (
        f"Exact human gate for PR #{number}: user authorizes exact head `{SHA_A}`; "
        "the approval becomes stale on any head or semantic change."
    )
    gate_id = api.add_external_comment(
        number,
        gate_body,
        created_at="2026-07-21T01:30:00Z",
    )
    pull.update(
        state="closed",
        merged=True,
        merge_commit_sha=SHA_B,
        merged_at="2026-07-21T02:00:00Z",
    )
    reconciliation_body = (
        f"Bounded production reconciliation is complete at deployed SHA `{SHA_C}`. "
        f"Machine-readable evidence fingerprint: `{EVIDENCE}`."
    )
    reconciliation_id = api.add_external_comment(
        number,
        reconciliation_body,
        created_at="2026-07-21T03:00:00Z",
    )
    command = (
        f"/wb-core production-mutation complete {number} "
        f"head {SHA_A} merge {SHA_B} deployed {SHA_C} "
        f"gate {gate_id} gate-digest {_body_fingerprint(gate_body)} "
        f"reconciliation {reconciliation_id} "
        f"reconciliation-digest {_body_fingerprint(reconciliation_body)} "
        f"evidence {EVIDENCE}"
    )
    api.comparisons = [{"status": "ahead", "behind_by": 0}]
    deploy_evidence = {
        "status": "reconciled",
        "healthy": True,
        "pr": number,
        "head": SHA_A,
        "merge": SHA_C,
        "expected_sha": SHA_C,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "read_only": True,
        "repairs_applied": False,
        "evidence": [{"operation": "readback", "runtime_sha": SHA_C}],
    }
    return api, command, deploy_evidence


def _assert_production_mutation_terminalization() -> None:
    completed: list[str] = []

    api, command_text, deploy_evidence = _production_mutation_terminal_fixture()
    command = parse_production_mutation_terminalization_command(command_text)
    plan = production_mutation_terminalization_preflight(
        api,
        command.pr,
        command,
        actor="orenvlad-ai",
        association="OWNER",
    )
    assert plan["status"] == "ready"
    unrelated = _pull(
        121,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(121)],
        created_at="2026-07-21T03:10:00Z",
        sha=SHA_C,
    )
    api.pulls[121] = unrelated
    _add_new_root_proof(api, 121)
    unrelated_snapshot = (
        set(_labels(unrelated)),
        list(api.list_comments(121)),
        str(unrelated["state"]),
    )
    assert complete_production_mutation_release(
        api,
        command.pr,
        command,
        deploy_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == PRODUCTION_LABEL
    terminal_pull = api.pulls[command.pr]
    assert _labels(terminal_pull) == {
        PRODUCTION_LABEL,
        STANDARD_TASK_LABEL,
        PRODUCTION_MUTATION_LABEL,
    }
    assert production_mutation_terminal_state_proven(api, terminal_pull)
    assert goal_disposition(api, command.pr).disposition == GoalDisposition.TERMINAL_SUCCESS
    assert (
        set(_labels(unrelated)),
        list(api.list_comments(121)),
        str(unrelated["state"]),
    ) == unrelated_snapshot
    completed.append("01_full_human_gate_deploy_reconcile_terminal_flow")

    comment_count = len(api.list_comments(command.pr))
    dispatch_count = len(api.dispatched)
    assert complete_production_mutation_release(
        api,
        command.pr,
        command,
        None,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "already-completed"
    assert len(api.list_comments(command.pr)) == comment_count
    assert len(api.dispatched) == dispatch_count + 1
    completed.append("02_repeat_delivery_is_idempotent")

    fail_closed_api = FakeApi()
    fail_closed_api.pulls[122] = _pull(
        122,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL],
        created_at="2026-07-21T04:00:00Z",
    )
    try:
        _prepare(fail_closed_api, 122)
    except ReleaseBlocked as exc:
        assert "human gate" in str(exc)
    else:
        raise AssertionError("production mutation must remain excluded from automatic release")
    assert not fail_closed_api.merges
    completed.append("03_ready_never_auto_merges_or_deploys")

    unauthorized_api, unauthorized_text, unauthorized_evidence = (
        _production_mutation_terminal_fixture(number=123)
    )
    unauthorized_command = parse_production_mutation_terminalization_command(
        unauthorized_text
    )
    for association in ("CONTRIBUTOR", "NONE"):
        try:
            production_mutation_terminalization_preflight(
                unauthorized_api,
                123,
                unauthorized_command,
                actor="outside",
                association=association,
            )
        except ReleaseBlocked as exc:
            assert "OWNER or MEMBER" in str(exc)
        else:
            raise AssertionError("unauthorized association must fail closed")
    try:
        complete_production_mutation_release(
            unauthorized_api,
            123,
            unauthorized_command,
            unauthorized_evidence,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=False,
        )
    except ReleaseBlocked as exc:
        assert "GitHub Actions" in str(exc)
    else:
        raise AssertionError("local/agent terminalization must be refused")
    completed.append("04_actor_association_and_actions_ownership_required")

    stale_api, stale_text, _ = _production_mutation_terminal_fixture(number=124)
    for stale_command_text, expected in (
        (stale_text.replace(f"head {SHA_A}", f"head {SHA_C}"), "head SHA is stale"),
        (stale_text.replace(f"merge {SHA_B}", f"merge {SHA_A}"), "merge SHA"),
    ):
        try:
            production_mutation_terminalization_preflight(
                stale_api,
                124,
                parse_production_mutation_terminalization_command(stale_command_text),
                actor="orenvlad-ai",
                association="OWNER",
            )
        except ReleaseBlocked as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("stale head/merge identity must fail closed")
    completed.append("05_stale_head_and_wrong_merge_fail_closed")

    missing_api, missing_text, missing_evidence = _production_mutation_terminal_fixture(
        number=125
    )
    missing_command = parse_production_mutation_terminalization_command(missing_text)
    missing_api.comment_metadata[missing_command.gate_comment_id][
        "author_association"
    ] = "CONTRIBUTOR"
    try:
        production_mutation_terminalization_preflight(
            missing_api,
            125,
            missing_command,
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "human gate" in str(exc)
    else:
        raise AssertionError("missing admissible human gate must fail closed")
    missing_api, missing_text, missing_evidence = _production_mutation_terminal_fixture(
        number=126
    )
    missing_command = parse_production_mutation_terminalization_command(missing_text)
    missing_api.update_comment(
        missing_command.reconciliation_comment_id,
        "reconciliation evidence was removed",
    )
    try:
        production_mutation_terminalization_preflight(
            missing_api,
            126,
            missing_command,
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "digest is stale" in str(exc)
    else:
        raise AssertionError("missing reconciliation/evidence must fail closed")
    baseline_api, baseline_text, _ = _production_mutation_terminal_fixture(
        number=131
    )
    baseline_api.checks = []
    try:
        production_mutation_terminalization_preflight(
            baseline_api,
            131,
            parse_production_mutation_terminalization_command(baseline_text),
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "successful baseline" in str(exc)
    else:
        raise AssertionError("missing exact-head baseline must fail closed")
    completed.append(
        "06_missing_baseline_gate_or_reconciliation_fails_closed"
    )

    deploy_api, deploy_text, deploy_evidence = _production_mutation_terminal_fixture(
        number=127
    )
    deploy_command = parse_production_mutation_terminalization_command(deploy_text)
    deploy_evidence["expected_sha"] = SHA_B
    try:
        complete_production_mutation_release(
            deploy_api,
            127,
            deploy_command,
            deploy_evidence,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
        )
    except ReleaseBlocked as exc:
        assert "deployment evidence" in str(exc)
    else:
        raise AssertionError("wrong deployed SHA evidence must fail closed")
    ancestor_api, ancestor_text, _ = _production_mutation_terminal_fixture(
        number=132
    )
    ancestor_api.comparisons = [{"status": "diverged", "behind_by": 1}]
    try:
        production_mutation_terminalization_preflight(
            ancestor_api,
            132,
            parse_production_mutation_terminalization_command(ancestor_text),
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "verified descendant" in str(exc)
    else:
        raise AssertionError("non-ancestor deployed SHA must fail closed")
    completed.append("07_wrong_deploy_evidence_fails_closed")

    wrong_api, wrong_text, wrong_evidence = _production_mutation_terminal_fixture(
        number=128
    )
    wrong_command = parse_production_mutation_terminalization_command(wrong_text)
    for labels in (
        {BLOCKED_LABEL, LOOP_TASK_LABEL, PRODUCTION_MUTATION_LABEL},
        {BLOCKED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL},
    ):
        _set_labels(wrong_api.pulls[128], labels)
        try:
            complete_production_mutation_release(
                wrong_api,
                128,
                wrong_command,
                wrong_evidence,
                actor="orenvlad-ai",
                association="OWNER",
                actions_owned=True,
            )
        except ReleaseBlocked as exc:
            assert "requires task:standard" in str(exc) or "requires scope:" in str(exc)
        else:
            raise AssertionError("wrong task/scope must fail closed")
    _set_labels(
        wrong_api.pulls[128],
        {BLOCKED_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL},
    )
    try:
        production_mutation_terminalization_preflight(
            wrong_api,
            128,
            parse_production_mutation_terminalization_command(
                wrong_text.replace(
                    "production-mutation complete 128",
                    "production-mutation complete 999",
                )
            ),
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "current PR" in str(exc)
    else:
        raise AssertionError("cross-PR terminalization must fail closed")
    completed.append("08_wrong_task_or_scope_fails_closed")

    forged_api, forged_text, _ = _production_mutation_terminal_fixture(number=129)
    forged_command = parse_production_mutation_terminalization_command(forged_text)
    forged_pull = forged_api.pulls[129]
    _set_labels(
        forged_pull,
        {PRODUCTION_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL},
    )
    genuine_marker = next(
        body
        for comment_number, body in api.comments
        if comment_number == command.pr
        and PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER in body
    ).replace("pr=120", "pr=129")
    forged_api.add_external_comment(
        129,
        genuine_marker,
        created_at="2026-07-21T04:00:00Z",
    )
    assert not production_mutation_terminal_state_proven(forged_api, forged_pull)
    assert goal_disposition(forged_api, 129).disposition == GoalDisposition.OWN_ACTION
    completed.append("09_agent_forged_marker_is_not_terminal_proof")

    action_api, action_text, _ = _production_mutation_terminal_fixture(number=130)
    fabricated_blocker = {
        "own_pr": 130,
        "action_pr": 130,
        "head_sha": SHA_A,
        "release_state": BLOCKED_LABEL,
        "loop_root": 0,
        "merge_sha": SHA_B,
        "attempts": ["old unsupported complete-standard contour"],
        "repo_owned_action_available": False,
        "remediation_exhausted": True,
        "terminal_failure": False,
        "user_intervention_required": True,
        "minimal_user_action": "manually edit labels",
    }
    action = goal_disposition(
        action_api,
        130,
        blocker_evidence=fabricated_blocker,
    )
    assert action.disposition == GoalDisposition.OWN_ACTION
    assert action.reason_code == "production-mutation-terminalization-available"
    assert "/wb-core production-mutation complete" in action.allowed_next_action
    completed.append("10_shepherd_prefers_repo_owned_terminalization")

    assert len(completed) == 10, completed
    print(f"production_mutation_terminalization: {len(completed)}/10 ok")


def _assert_ack_invalidated_by_head_change() -> None:
    api = FakeApi()
    loop = _pull(
        30,
        labels=[AWAITING_AGENT_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T08:00:00Z",
    )
    api.pulls[30] = loop
    _add_new_root_proof(api, 30)
    acknowledge_loop_agent(api, 30, SHA_A, actor="codex", association="OWNER")
    loop["head"]["sha"] = SHA_B
    candidate = _prepare(api, 30)
    assert not candidate.agent_acknowledged
    request_loop_agent(api, 30, SHA_B)
    assert not any(label.startswith(LOOP_ACK_PREFIX) for label in _labels(loop))
    assert AWAITING_AGENT_LABEL in _labels(loop)


def _assert_waiter_contract() -> None:
    class AdvancingWaitApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.target_reads = 0
            self.foreign_released = False

        def get_pull(self, number: int) -> dict[str, Any]:
            if number == 81:
                self.target_reads += 1
                if self.target_reads == 2:
                    _set_labels(
                        self.pulls[80],
                        transition_label_set(_labels(self.pulls[80]), PRODUCTION_LABEL),
                    )
                    _set_labels(
                        self.pulls[81],
                        transition_label_set(
                            transition_label_set(_labels(self.pulls[81]), RUNNING_LABEL),
                            AWAITING_AGENT_LABEL,
                        ),
                    )
                    self.foreign_released = True
            return super().get_pull(number)

        def add_comment(self, number: int, body: str) -> None:
            super().add_comment(number, body)
            if number == 81 and body.startswith("/wb-core loop ack-agent"):
                self.pulls[81]["state"] = "closed"
                self.pulls[81]["merged"] = True
                self.pulls[81]["merge_commit_sha"] = SHA_B
                super().add_comment(
                    81,
                    "<!-- wb-core-loop-chain-audit merge="
                    + SHA_B
                    + " root=81 terminal_pr=81 -->",
                )
                _set_labels(
                    self.pulls[81],
                    transition_label_set(
                        transition_label_set(
                            transition_label_set(
                                transition_label_set(
                                    _labels(self.pulls[81]), READY_LABEL
                                ),
                                RUNNING_LABEL,
                            ),
                            AWAITING_UI_LABEL,
                        ),
                        PRODUCTION_LABEL,
                    )
                    | {loop_root_label(81)},
                )

    api = AdvancingWaitApi()
    foreign = _pull(
        80,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(80)],
        created_at="2026-07-17T09:00:00Z",
    )
    foreign["state"] = "closed"
    foreign["merged"] = True
    foreign["merge_commit_sha"] = SHA_A
    loop = _pull(
        81,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T09:01:00Z",
    )
    api.pulls = {80: foreign, 81: loop}
    _add_new_root_proof(api, 80)
    api.add_comment(80, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=80 root=80 -->")
    _add_new_root_proof(api, 81)
    output: list[str] = []
    code = wait_for_release(
        api,
        81,
        status_seconds=0,
        poll_seconds=0,
        acknowledge_agent=True,
        emit=output.append,
    )
    assert code == 0
    assert api.foreign_released
    assert any(
        comment == (81, f"/wb-core loop ack-agent 81 head {SHA_A}")
        for comment in api.comments
    )
    assert any("normal queue waiting continues" in line for line in output)
    assert not any("fail-closed" in line for line in output)

    ui_api = FakeApi()
    ui_loop = _pull(
        82,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(82)],
        created_at="2026-07-17T09:02:00Z",
    )
    ui_api.pulls[82] = ui_loop
    _add_new_root_proof(ui_api, 82)
    ui_loop["state"] = "closed"
    ui_loop["merged"] = True
    ui_loop["merge_commit_sha"] = SHA_A
    ui_api.add_comment(
        82, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=82 root=82 -->"
    )
    assert (
        wait_for_release(
            ui_api,
            82,
            status_seconds=0,
            poll_seconds=0,
            acknowledge_agent=True,
            emit=output.append,
        )
        == EXIT_AWAITING_UI
    )

    superseded_api = FakeApi()
    superseded_api.pulls[83] = _pull(
        83,
        labels=[SUPERSEDED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(82)],
        created_at="2026-07-17T09:03:00Z",
    )
    comments_before = list(superseded_api.comments)
    assert (
        wait_for_release(
            superseded_api,
            83,
            status_seconds=0,
            poll_seconds=0,
            acknowledge_agent=True,
            emit=output.append,
        )
        == 2
    )
    assert superseded_api.comments == comments_before
    assert any("terminal: release:superseded" in line for line in output)

    classification_api = FakeApi()
    classification_api.pulls[84] = _pull(
        84,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(84)],
        created_at="2026-07-17T09:04:00Z",
    )
    labels_before = _labels(classification_api.pulls[84])
    comments_before = list(classification_api.comments)
    classification_output: list[str] = []
    assert (
        wait_for_release(
            classification_api,
            84,
            status_seconds=0,
            poll_seconds=0,
            acknowledge_agent=True,
            emit=classification_output.append,
        )
        == 2
    )
    assert _labels(classification_api.pulls[84]) == labels_before
    assert classification_api.comments == comments_before
    assert any("fail-closed classification" in line for line in classification_output)


def _goal_gate_fixture(*, lost_owner: bool) -> tuple[FakeApi, dict[str, Any], dict[str, Any]]:
    """Synthetic predecessor/successor fixture; never reads or mutates real regression PRs."""

    api = FakeApi()
    predecessor_number = 710
    successor_number = 711
    predecessor = _pull(
        predecessor_number,
        labels=[
            AWAITING_UI_LABEL,
            LOOP_TASK_LABEL,
            LIVE_RUNTIME_LABEL,
            loop_root_label(predecessor_number),
        ],
        created_at="2026-07-20T05:00:00Z",
        sha=SHA_A,
    )
    predecessor["state"] = "closed"
    predecessor["merged"] = True
    predecessor["merge_commit_sha"] = SHA_A
    successor = _pull(
        successor_number,
        labels=[
            READY_LABEL,
            LOOP_TASK_LABEL,
            LIVE_RUNTIME_LABEL,
            loop_root_label(successor_number),
        ],
        created_at="2026-07-20T05:01:00Z",
        sha=SHA_B,
    )
    api.pulls = {predecessor_number: predecessor, successor_number: successor}
    _add_new_root_proof(api, predecessor_number)
    _add_new_root_proof(api, successor_number)
    api.add_comment(
        predecessor_number,
        f"<!-- wb-core-loop-deploy-proof merge={SHA_A} "
        f"pr={predecessor_number} root={predecessor_number} -->",
    )
    upsert_status_comment(
        api,
        successor_number,
        owner="successor-agent",
        reason="synthetic queued owner",
        last_action="synthetic successor heartbeat",
        intervention=False,
    )
    if lost_owner:
        api.add_labels(predecessor_number, [NEEDS_RESUME_LABEL])
        upsert_status_comment(
            api,
            predecessor_number,
            owner="unowned",
            reason="synthetic lost owner",
            last_action="synthetic release:needs-resume proof",
            intervention=True,
        )
    else:
        upsert_status_comment(
            api,
            predecessor_number,
            owner="active-agent",
            reason="synthetic live owner",
            last_action="synthetic heartbeat",
            intervention=False,
        )
    return api, predecessor, successor


def _assert_goal_shepherd_regressions() -> None:
    completed: list[str] = []
    assert {item.value for item in GoalDisposition} == {
        "TERMINAL_SUCCESS",
        "CONTINUE_WAITING",
        "CONTINUE_SAFE_PHASES",
        "AWAIT_PHASE_CAPABILITY",
        "OWN_ACTION",
        "TAKEOVER_PREDECESSOR",
        "RECOVER_OWN_CHAIN",
        "EXTERNAL_BLOCKER",
        "TERMINAL_FAILURE",
    }

    lost_api, predecessor, successor = _goal_gate_fixture(lost_owner=True)
    predecessor_number = int(predecessor["number"])
    successor_number = int(successor["number"])
    takeover = goal_disposition(lost_api, successor_number)
    assert takeover.disposition == GoalDisposition.TAKEOVER_PREDECESSOR
    assert takeover.own_pr == successor_number and takeover.action_pr == predecessor_number
    assert takeover.allowed_next_action == (
        f"python3 apps/github_release_train_wait.py {predecessor_number} "
        "--resume-owner --no-ack-agent"
    )
    assert takeover.user_intervention_required is False
    assert takeover.remediation_exhausted is False
    assert set(takeover.as_dict()) == {
        "disposition",
        "own_pr",
        "action_pr",
        "canonical_github_state",
        "reason_code",
        "allowed_next_action",
        "user_intervention_required",
        "evidence",
        "remediation_exhausted",
        "current_phase",
        "blocked_phase",
        "safe_phases_remaining",
        "required_capability",
        "capability_evidence",
        "next_executable_action",
    }
    identity = next(item for item in takeover.evidence if item.get("kind") == "loop-identity")
    assert identity["exact_head_verified"] is True
    assert identity["exact_deployed_sha"] == SHA_A
    assert identity["exact_deployed_sha_verified"] is True
    assert identity["own_root"] == successor_number
    assert identity["action_root"] == predecessor_number
    assert identity["root_isolation_preserved"] is True
    live_overlay_api, live_overlay_predecessor, live_overlay_successor = _goal_gate_fixture(
        lost_owner=True
    )
    upsert_status_comment(
        live_overlay_api,
        int(live_overlay_predecessor["number"]),
        owner="returned-owner",
        reason="newer synthetic owner heartbeat",
        last_action="owner returned before takeover",
        intervention=False,
        now=datetime.now(timezone.utc).timestamp() + 1,
    )
    assert goal_disposition(
        live_overlay_api, int(live_overlay_successor["number"])
    ).disposition == GoalDisposition.OWN_ACTION
    completed.append("01_lost_predecessor_takeover_not_blocked")

    live_api, live_predecessor, live_successor = _goal_gate_fixture(lost_owner=False)
    waiting = goal_disposition(live_api, int(live_successor["number"]))
    assert waiting.disposition == GoalDisposition.CONTINUE_WAITING
    assert waiting.action_pr == int(live_predecessor["number"])
    completed.append("02_live_predecessor_normal_waiting")

    for observations in (3, 30, 300):
        decisions = {
            goal_disposition(live_api, int(live_successor["number"])).disposition
            for _ in range(observations)
        }
        assert decisions == {GoalDisposition.CONTINUE_WAITING}
    fabricated_waiting_blocker = {
        "own_pr": int(live_successor["number"]),
        "action_pr": int(live_successor["number"]),
        "canonical_reason_code": "fabricated-waiting-blocker",
        "attempts": ["waited"],
        "repo_owned_action_available": False,
        "remediation_exhausted": True,
        "terminal_failure": False,
        "user_intervention_required": True,
        "minimal_user_action": "intervene",
    }
    assert goal_disposition(
        live_api,
        int(live_successor["number"]),
        blocker_evidence=fabricated_waiting_blocker,
    ).disposition == GoalDisposition.CONTINUE_WAITING
    assert shepherd_release(
        live_api,
        int(live_successor["number"]),
        status_seconds=0,
        poll_seconds=0,
        once=True,
        emit=lambda _: None,
    ) == EXIT_CONTINUE_WAITING
    completed.append("03_repetition_never_blocks")

    assert resume_loop_owner(
        lost_api,
        predecessor_number,
        SHA_A,
        predecessor_number,
        actor="takeover-agent",
        association="OWNER",
    ) == "resumed"
    resumed = goal_disposition(lost_api, successor_number)
    assert resumed.disposition == GoalDisposition.CONTINUE_WAITING
    assert resumed.action_pr == predecessor_number
    assert accept_loop_ui(
        lost_api,
        predecessor_number,
        actor="takeover-agent",
        association="OWNER",
        deployed_sha=SHA_A,
        evidence=EVIDENCE,
    ) == "accepted"
    after_predecessor = goal_disposition(lost_api, successor_number)
    assert after_predecessor.disposition == GoalDisposition.CONTINUE_WAITING
    assert after_predecessor.action_pr == successor_number
    assert select_candidate(lost_api)["pr_number"] == successor_number
    completed.append("04_takeover_accept_then_return_to_own_queue")

    intermediate_api, _, intermediate_successor = _goal_gate_fixture(lost_owner=True)
    before_comments = list(intermediate_api.comments)
    exit_code = shepherd_release(
        intermediate_api,
        int(intermediate_successor["number"]),
        status_seconds=0,
        poll_seconds=0,
        once=False,
        emit=lambda _: None,
    )
    assert exit_code == EXIT_RESUMED and exit_code != 2
    assert intermediate_api.comments == before_comments
    completed.append("05_takeover_exit_is_intermediate")

    defect_api, defect_gate, defect_successor = _goal_gate_fixture(lost_owner=False)
    gate_number = int(defect_gate["number"])
    successor_snapshot = (_labels(defect_successor), list(defect_api.list_comments(int(defect_successor["number"]))))
    recovery_number = 712
    defect_api.pulls[recovery_number] = _pull(
        recovery_number,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T05:02:00Z",
        sha=SHA_C,
    )
    defect_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    enqueue_loop_recovery(
        defect_api,
        recovery_number,
        SHA_C,
        gate_pr=gate_number,
        expected_root=gate_number,
        actor="ui-agent",
        association="OWNER",
    )
    assert select_candidate(defect_api)["pr_number"] == recovery_number
    assert AWAITING_UI_LABEL in _labels(defect_gate)
    assert goal_disposition(
        defect_api, int(defect_successor["number"])
    ).disposition == GoalDisposition.CONTINUE_WAITING
    assert successor_snapshot == (
        _labels(defect_successor),
        list(defect_api.list_comments(int(defect_successor["number"]))),
    )
    completed.append("06_ui_defect_recovery_keeps_successor_healthy")

    runtime = select_ui_runtime(
        execution_surface="codex-cli",
        playwright_available=True,
        chromium_launchable=True,
        repo_owned_recovery_available=False,
    )
    assert runtime.runtime == UiRuntime.LOCAL_PLAYWRIGHT
    assert runtime.continue_ui_flow and not runtime.external_blocker_eligible
    report = local_playwright_preflight(own_pr=successor_number, launch_probe=lambda: None)
    assert report["runtime"] == UiRuntime.LOCAL_PLAYWRIGHT.value
    assert report["embedded_browser_required"] is False
    assert report["continue_ui_flow"] is True
    completed.append("07_embedded_browser_absence_is_irrelevant")

    recoverable_runtime = select_ui_runtime(
        execution_surface="codex-cli",
        playwright_available=True,
        chromium_launchable=False,
        repo_owned_recovery_available=True,
    )
    assert not recoverable_runtime.continue_ui_flow
    assert not recoverable_runtime.external_blocker_eligible
    ui_api, ui_gate, _ = _goal_gate_fixture(lost_owner=False)
    ui_number = int(ui_gate["number"])
    assert goal_disposition(ui_api, ui_number).disposition == GoalDisposition.RECOVER_OWN_CHAIN
    assert shepherd_release(
        ui_api,
        ui_number,
        status_seconds=0,
        poll_seconds=0,
        once=False,
        emit=lambda _: None,
    ) == EXIT_AWAITING_UI
    chromium_blocker = {
        "own_pr": ui_number,
        "action_pr": ui_number,
        "head_sha": SHA_A,
        "release_state": AWAITING_UI_LABEL,
        "loop_root": ui_number,
        "merge_sha": SHA_A,
        "canonical_reason_code": "local-chromium-unavailable-after-recovery",
        "attempts": [
            "Playwright import preflight",
            "isolated Chromium launch",
            "repo-owned browser restore",
        ],
        "repo_owned_action_available": False,
        "remediation_exhausted": True,
        "terminal_failure": False,
        "user_intervention_required": True,
        "minimal_user_action": "grant the missing local browser installation permission",
    }
    blocked_ui = goal_disposition(ui_api, ui_number, blocker_evidence=chromium_blocker)
    assert blocked_ui.disposition == GoalDisposition.EXTERNAL_BLOCKER
    assert blocked_ui.remediation_exhausted and blocked_ui.user_intervention_required
    completed.append("08_chromium_blocker_requires_exhausted_preflight")

    blocked_api = FakeApi()
    blocked_number = 720
    blocked_api.pulls[blocked_number] = _pull(
        blocked_number,
        labels=[BLOCKED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-20T05:10:00Z",
    )
    own_blocked = goal_disposition(blocked_api, blocked_number)
    assert own_blocked.disposition == GoalDisposition.OWN_ACTION
    assert own_blocked.remediation_exhausted is False
    assert shepherd_release(
        blocked_api,
        blocked_number,
        status_seconds=0,
        poll_seconds=0,
        once=False,
        emit=lambda _: None,
    ) == EXIT_OWN_ACTION
    blocked_external_evidence = {
        "own_pr": blocked_number,
        "action_pr": blocked_number,
        "head_sha": SHA_A,
        "release_state": BLOCKED_LABEL,
        "loop_root": 0,
        "merge_sha": "",
        "canonical_reason_code": "bounded-fix-needs-external-approval",
        "attempts": ["bounded diagnosis", "exact-head repair", "trusted retry"],
        "repo_owned_action_available": False,
        "remediation_exhausted": True,
        "terminal_failure": False,
        "user_intervention_required": True,
        "minimal_user_action": "grant the missing repository approval",
    }
    assert goal_disposition(
        blocked_api,
        blocked_number,
        blocker_evidence=blocked_external_evidence,
    ).disposition == GoalDisposition.EXTERNAL_BLOCKER
    assert shepherd_release(
        blocked_api,
        blocked_number,
        status_seconds=0,
        poll_seconds=0,
        once=False,
        blocker_evidence=blocked_external_evidence,
        emit=lambda _: None,
    ) == EXIT_BLOCKED
    terminal_failure_evidence = dict(
        blocked_external_evidence,
        terminal_failure=True,
        user_intervention_required=False,
        minimal_user_action="",
        canonical_reason_code="proven-protocol-irrecoverable",
    )
    assert shepherd_release(
        blocked_api,
        blocked_number,
        status_seconds=0,
        poll_seconds=0,
        once=False,
        blocker_evidence=terminal_failure_evidence,
        emit=lambda _: None,
    ) == EXIT_TERMINAL_FAILURE
    invalid_available_evidence = dict(
        blocked_external_evidence,
        repo_owned_action_available=True,
    )
    try:
        goal_disposition(
            blocked_api,
            blocked_number,
            blocker_evidence=invalid_available_evidence,
        )
    except ValueError as exc:
        assert "no repo-owned action" in str(exc)
    else:
        raise AssertionError("repo-owned remediation must forbid EXTERNAL_BLOCKER")
    stale_root_api = FakeApi()
    _terminal_loop_fixture(stale_root_api, 730)
    stale_root_api.pulls[731] = _pull(
        731,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(730)],
        created_at="2026-07-20T05:10:30Z",
        sha=SHA_B,
    )
    assert goal_disposition(stale_root_api, 731).disposition == GoalDisposition.OWN_ACTION
    completed.append("09_own_blocked_requires_remediation_exhaustion")

    halted_api = FakeApi()
    halted_number = 721
    halted = _pull(
        halted_number,
        labels=[HALTED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T05:11:00Z",
        sha=SHA_B,
    )
    halted["state"] = "closed"
    halted["merged"] = True
    halted["merge_commit_sha"] = SHA_A
    halted_api.pulls[halted_number] = halted
    assert goal_disposition(halted_api, halted_number).disposition == GoalDisposition.OWN_ACTION
    reconciliation = {
        "status": "reconciled",
        "healthy": True,
        "pr": halted_number,
        "head": SHA_B,
        "merge": SHA_A,
        "expected_sha": SHA_A,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
    }
    assert resume_halted_release(halted_api, halted_number, reconciliation) == "production"
    assert goal_disposition(halted_api, halted_number).disposition == GoalDisposition.TERMINAL_SUCCESS
    assert shepherd_release(
        halted_api,
        halted_number,
        status_seconds=0,
        poll_seconds=0,
        once=False,
        emit=lambda _: None,
    ) == 0

    failed_halted_api = FakeApi()
    failed_halted = _pull(
        halted_number + 1,
        labels=[HALTED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T05:12:00Z",
        sha=SHA_B,
    )
    failed_halted["state"] = "closed"
    failed_halted["merged"] = True
    failed_halted["merge_commit_sha"] = SHA_A
    failed_halted_api.pulls[halted_number + 1] = failed_halted
    failed_evidence = {
        "own_pr": halted_number + 1,
        "action_pr": halted_number + 1,
        "head_sha": SHA_B,
        "release_state": HALTED_LABEL,
        "loop_root": 0,
        "merge_sha": SHA_A,
        "canonical_reason_code": "exact-sha-reconciliation-external-permission",
        "attempts": ["bounded exact-SHA reconciliation", "idempotent retry"],
        "repo_owned_action_available": False,
        "remediation_exhausted": True,
        "terminal_failure": False,
        "user_intervention_required": True,
        "minimal_user_action": "restore the missing GitHub Environment approval",
    }
    assert goal_disposition(
        failed_halted_api,
        halted_number + 1,
        blocker_evidence=failed_evidence,
    ).disposition == GoalDisposition.EXTERNAL_BLOCKER
    completed.append("10_halted_reconcile_or_evidence_blocker")

    serial_api, serial_gate, serial_successor = _goal_gate_fixture(lost_owner=False)
    queue = select_candidate(serial_api)
    assert queue["status"] == "awaiting-ui" and not queue["found"]
    assert goal_disposition(
        serial_api, int(serial_successor["number"])
    ).action_pr == int(serial_gate["number"])
    completed.append("11_independent_roots_stay_serialized")

    no_auto_api, no_auto_predecessor, no_auto_successor = _goal_gate_fixture(lost_owner=True)
    comments_before = list(no_auto_api.comments)
    takeover_decision = goal_disposition(no_auto_api, int(no_auto_successor["number"]))
    assert takeover_decision.disposition == GoalDisposition.TAKEOVER_PREDECESSOR
    assert no_auto_api.comments == comments_before
    assert not any(
        "/wb-core loop ack-agent" in body or "/wb-core loop accept-ui" in body
        for _, body in no_auto_api.comments
    )
    assert NEEDS_RESUME_LABEL in _labels(no_auto_predecessor)
    completed.append("12_takeover_never_acknowledges_or_accepts")

    protocol_sources = [
        (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md").read_text(
            encoding="utf-8"
        ),
        (ROOT / "docs" / "architecture" / "11_github_release_train.md").read_text(
            encoding="utf-8"
        ),
        (ROOT / "apps" / "github_release_train_wait.py").read_text(encoding="utf-8"),
    ]
    for source in protocol_sources:
        lowered = source.casefold()
        assert "открой встроенный browser" not in lowered
        assert "open the embedded browser" not in lowered
    for source in protocol_sources[:3]:
        for required in (
            "TERMINAL_SUCCESS",
            "CONTINUE_WAITING",
            "CONTINUE_SAFE_PHASES",
            "AWAIT_PHASE_CAPABILITY",
            "OWN_ACTION",
            "TAKEOVER_PREDECESSOR",
            "RECOVER_OWN_CHAIN",
            "EXTERNAL_BLOCKER",
            "TERMINAL_FAILURE",
            "remediation_exhausted",
            "current_phase",
            "blocked_phase",
            "safe_phases_remaining",
            "required_capability",
            "capability_evidence",
            "next_executable_action",
            "--shepherd",
            "--playwright-preflight",
        ):
            assert required in source
    assert all(
        ("local" in source.casefold() or "локаль" in source.casefold())
        and "playwright" in source.casefold()
        for source in protocol_sources
    )
    help_text = build_parser().format_help()
    for required in (
        "--shepherd",
        "--once",
        "--blocker-evidence",
        "--phase-state",
        "--playwright-preflight",
        "2 proven EXTERNAL_BLOCKER",
        "6 --once normal waiting",
        "7 proven TERMINAL_FAILURE",
        "CONTINUE_SAFE_PHASES",
        "AWAIT_PHASE_CAPABILITY",
        "Elapsed time is",
        "never terminal",
    ):
        assert required in help_text
    completed.append("13_cli_protocol_never_requires_embedded_browser")

    this_source = Path(__file__).read_text(encoding="utf-8")
    for real_pr_number in (600 + 84, 700 - 10):
        assert f"#{real_pr_number}" not in this_source
    completed.append("14_regression_prs_are_synthetic_only")

    assert len(completed) == 14, completed
    print(f"goal_shepherd_regressions: {len(completed)}/14 ok")


def _assert_phase_local_goal_regressions() -> None:
    completed: list[str] = []
    canonical = {
        "own": {"pr": 810, "release_state": "release:none", "head_sha": SHA_A},
        "queue": {"status": "idle", "gate_pr": 0},
    }

    def classify(context: GoalPhaseContext):
        decision = phase_goal_decision(
            context,
            own_pr=810,
            action_pr=810,
            canonical_github_state=canonical,
        )
        assert decision is not None
        return decision

    repository_work = (
        GoalPhase.REPOSITORY_PREFLIGHT,
        GoalPhase.REPOSITORY_IMPLEMENTATION,
        GoalPhase.REPOSITORY_VALIDATION,
        GoalPhase.REPOSITORY_RUNNER_PREPARATION,
        GoalPhase.PULL_REQUEST,
    )
    missing_credentials = classify(
        GoalPhaseContext(
            current_phase=GoalPhase.REPOSITORY_PREFLIGHT,
            safe_phases_remaining=repository_work,
            required_capability=GoalCapability.PRODUCTION_CREDENTIALS.value,
            capability_available=False,
            next_executable_action="inspect repository and implement the fixture-backed runner",
        )
    )
    assert missing_credentials.disposition == GoalDisposition.CONTINUE_SAFE_PHASES
    assert missing_credentials.blocked_phase is None
    assert GoalPhase.PULL_REQUEST in missing_credentials.safe_phases_remaining
    completed.append("01_future_backfill_credentials_do_not_block_repository")

    missing_mcp = classify(
        GoalPhaseContext.from_mapping(
            {
                "current_phase": GoalPhase.REPOSITORY_IMPLEMENTATION.value,
                "safe_phases_remaining": [GoalPhase.REPOSITORY_IMPLEMENTATION.value],
                "required_capability": GoalCapability.WEBCORE_DATA_MCP_READ.value,
                "capability_available": False,
            }
        )
    )
    assert missing_mcp.disposition == GoalDisposition.CONTINUE_SAFE_PHASES
    completed.append("02_missing_mcp_is_irrelevant_to_repository_phase")

    mcp_allowlist = {GoalCapability.PRODUCTION_READ.value}
    assert not mcp_capability_sufficient(
        GoalCapability.PRODUCTION_MANIFEST,
        mcp_allowlist,
    )
    assert not mcp_capability_sufficient(
        GoalCapability.PRODUCTION_DIGEST,
        mcp_allowlist,
    )
    assert production_evidence_route(
        GoalCapability.PRODUCTION_MANIFEST,
        mcp_allowlist=mcp_allowlist,
    ) == "repo-owned-runner"
    assert not mcp_capability_sufficient(
        GoalCapability.PRODUCTION_MUTATION,
        {GoalCapability.PRODUCTION_MUTATION.value},
    )
    completed.append("03_mcp_allowlist_is_checked_per_capability")

    no_browser_session = classify(
        GoalPhaseContext(
            current_phase=GoalPhase.REPOSITORY_IMPLEMENTATION,
            safe_phases_remaining=(
                GoalPhase.REPOSITORY_IMPLEMENTATION,
                GoalPhase.REPOSITORY_VALIDATION,
                GoalPhase.PULL_REQUEST,
            ),
            required_capability=GoalCapability.PRODUCTION_UI_AUTH.value,
            capability_available=False,
        )
    )
    assert no_browser_session.disposition == GoalDisposition.CONTINUE_SAFE_PHASES
    assert GoalPhase.PULL_REQUEST in no_browser_session.safe_phases_remaining
    completed.append("04_future_browser_auth_does_not_block_development")

    apply_count = 0
    missing_backup = GoalPhaseContext(
        current_phase=GoalPhase.PRODUCTION_MUTATION_PREFLIGHT,
        safe_phases_remaining=(),
        required_capability="production-backup-and-pre-change-digest",
        capability_available=False,
        capability_evidence=(
            {
                "kind": "production-mutation-preflight",
                "backup_available": False,
                "pre_change_digest_available": False,
                "attempts": ["canonical runner dry-run", "backup evidence lookup"],
                "repo_owned_action_available": False,
            },
        ),
        remediation_exhausted=True,
        user_intervention_required=True,
        minimal_user_action="restore read access to the canonical backup evidence",
    )
    pre_apply_wait = classify(missing_backup)
    assert pre_apply_wait.disposition == GoalDisposition.AWAIT_PHASE_CAPABILITY
    assert pre_apply_wait.blocked_phase == GoalPhase.PRODUCTION_MUTATION_PREFLIGHT
    incomplete_runner = {
        requirement: True
        for requirement in PRODUCTION_MUTATION_RUNNER_REQUIREMENTS
        if requirement not in {"pre_change_digest", "backup_evidence_contract"}
    }
    incomplete_contract = production_mutation_runner_contract(incomplete_runner)
    assert incomplete_contract["apply_allowed"] is False
    assert incomplete_contract["missing_requirements"] == [
        "backup_evidence_contract",
        "pre_change_digest",
    ]
    assert apply_count == 0
    completed.append("05_missing_backup_fails_closed_only_before_apply")

    restored = classify(
        GoalPhaseContext(
            current_phase=GoalPhase.PRODUCTION_MUTATION_PREFLIGHT,
            required_capability="production-backup-and-pre-change-digest",
            capability_available=True,
            capability_evidence=(
                {
                    "kind": "production-mutation-preflight",
                    "backup_available": True,
                    "pre_change_digest_available": True,
                    "repo_owned_action_available": True,
                },
            ),
            next_executable_action="run the canonical runner once with explicit --apply",
        )
    )
    assert restored.disposition == GoalDisposition.OWN_ACTION
    manifest = {requirement: True for requirement in PRODUCTION_MUTATION_RUNNER_REQUIREMENTS}
    manifest["operation_id"] = "synthetic-fixture-operation"
    runner_contract = production_mutation_runner_contract(manifest)
    assert runner_contract == {
        "valid": True,
        "missing_requirements": [],
        "apply_allowed": True,
    }
    applied_operations: set[str] = set()
    for _ in range(2):
        if manifest["operation_id"] not in applied_operations:
            apply_count += 1
            applied_operations.add(str(manifest["operation_id"]))
    reconciliation = {
        "operation_id": manifest["operation_id"],
        "affected_records": 2,
        "expected_affected_records": 2,
        "non_target_invariants_preserved": True,
    }
    assert apply_count == 1
    assert reconciliation["affected_records"] == reconciliation["expected_affected_records"]
    assert reconciliation["non_target_invariants_preserved"] is True
    assert set(PRODUCTION_MUTATION_RUNNER_REQUIREMENTS) <= set(manifest)
    completed.append("06_restored_capability_applies_once_and_reconciles")

    prompt_order = order_goal_phases(
        (
            GoalPhase.PRODUCTION_MUTATION_PREFLIGHT,
            GoalPhase.PRODUCTION_UI_PREFLIGHT,
            GoalPhase.REPOSITORY_PREFLIGHT,
            GoalPhase.PULL_REQUEST,
            GoalPhase.REPOSITORY_IMPLEMENTATION,
        )
    )
    assert prompt_order[:3] == (
        GoalPhase.REPOSITORY_PREFLIGHT,
        GoalPhase.REPOSITORY_IMPLEMENTATION,
        GoalPhase.PULL_REQUEST,
    )
    assert prompt_order.index(GoalPhase.PRODUCTION_MUTATION_PREFLIGHT) > prompt_order.index(
        GoalPhase.PULL_REQUEST
    )
    completed.append("07_prompt_order_is_rebuilt_from_dependencies")

    future_database = classify(
        GoalPhaseContext(
            current_phase=GoalPhase.REPOSITORY_VALIDATION,
            safe_phases_remaining=(
                GoalPhase.REPOSITORY_VALIDATION,
                GoalPhase.REPOSITORY_RUNNER_PREPARATION,
            ),
            required_capability=GoalCapability.PRODUCTION_DATABASE.value,
            capability_available=False,
            capability_evidence=(
                {
                    "kind": "future-capability",
                    "repo_owned_action_available": False,
                },
            ),
            remediation_exhausted=True,
            user_intervention_required=True,
            minimal_user_action="provide future production database authorization",
        )
    )
    assert future_database.disposition == GoalDisposition.CONTINUE_SAFE_PHASES
    assert not future_database.user_intervention_required
    completed.append("08_future_missing_capability_keeps_safe_work_running")

    phase_api = FakeApi()
    phase_api.pulls[810] = _pull(
        810,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-20T06:00:00Z",
        sha=SHA_A,
    )
    immediate_auth = GoalPhaseContext(
        current_phase=GoalPhase.PRODUCTION_READ_PREFLIGHT,
        required_capability=GoalCapability.PRODUCTION_CREDENTIALS.value,
        capability_available=False,
        capability_evidence=(
            {
                "kind": "production-read-preflight",
                "authentication_result": "permission-denied",
                "attempts": ["canonical read-only runner preflight"],
                "repo_owned_action_available": False,
            },
        ),
        remediation_exhausted=True,
        user_intervention_required=True,
        minimal_user_action="grant read-only access for the named production evidence source",
    )
    awaited = goal_disposition(phase_api, 810, phase_context=immediate_auth)
    assert awaited.disposition == GoalDisposition.AWAIT_PHASE_CAPABILITY
    assert awaited.user_intervention_required and awaited.remediation_exhausted
    assert awaited.safe_phases_remaining == ()
    assert shepherd_release(
        phase_api,
        810,
        status_seconds=0,
        poll_seconds=0,
        once=True,
        phase_context=immediate_auth,
        emit=lambda _: None,
    ) == EXIT_AWAIT_PHASE_CAPABILITY
    completed.append("09_immediate_external_auth_awaits_exact_capability")

    for keyword, capability in (
        ("MCP", GoalCapability.WEBCORE_DATA_MCP_READ),
        ("browser", GoalCapability.LOCAL_PLAYWRIGHT),
        ("credentials", GoalCapability.PRODUCTION_CREDENTIALS),
        ("production database", GoalCapability.PRODUCTION_DATABASE),
    ):
        keyword_decision = classify(
            GoalPhaseContext(
                current_phase=GoalPhase.REPOSITORY_PREFLIGHT,
                safe_phases_remaining=(GoalPhase.REPOSITORY_PREFLIGHT,),
                required_capability=capability.value,
                capability_available=False,
                capability_evidence=(
                    {
                        "kind": "keyword-only",
                        "keyword": keyword,
                        "repo_owned_action_available": False,
                    },
                ),
            )
        )
        assert keyword_decision.disposition == GoalDisposition.CONTINUE_SAFE_PHASES
        assert keyword_decision.disposition != GoalDisposition.EXTERNAL_BLOCKER
    assumption_only = classify(
        GoalPhaseContext(
            current_phase=GoalPhase.PRODUCTION_READ_PREFLIGHT,
            required_capability=GoalCapability.PRODUCTION_CREDENTIALS.value,
            capability_available=False,
            capability_evidence=(
                {
                    "kind": "keyword-only",
                    "keyword": "credentials",
                    "repo_owned_action_available": False,
                },
            ),
            remediation_exhausted=True,
            user_intervention_required=True,
            minimal_user_action="provide credentials",
        )
    )
    assert assumption_only.disposition == GoalDisposition.OWN_ACTION
    assert assumption_only.reason_code == "phase-capability-preflight-required"
    phase_protocol_sources = [
        (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md").read_text(
            encoding="utf-8"
        ),
        (ROOT / "docs" / "architecture" / "11_github_release_train.md").read_text(
            encoding="utf-8"
        ),
    ]
    for source in phase_protocol_sources:
        for required in (
            "REPOSITORY_PREFLIGHT",
            "PRODUCTION_READ_PREFLIGHT",
            "PRODUCTION_MUTATION_PREFLIGHT",
            "PRODUCTION_UI_PREFLIGHT",
            "CONTINUE_SAFE_PHASES",
            "AWAIT_PHASE_CAPABILITY",
            "current_phase",
            "blocked_phase",
            "safe_phases_remaining",
            "required_capability",
            "capability_evidence",
            "next_executable_action",
            "query-only",
            "repo-owned runner",
            "dry-run",
            "fixtures/mocks",
        ):
            assert required in source
    assert EXIT_CONTINUE_SAFE_PHASES == 8
    completed.append("10_capability_words_never_create_global_blocker")

    assert len(completed) == 10, completed
    print(f"phase_local_goal_regressions: {len(completed)}/10 ok")


def _assert_workflow_contract() -> None:
    baseline = (ROOT / ".github" / "workflows" / "baseline-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release-train.yml").read_text(encoding="utf-8")
    implementation = "\n".join(
        (
            (ROOT / "apps" / "github_release_train.py").read_text(encoding="utf-8"),
            (ROOT / "apps" / "github_release_train_spec.py").read_text(encoding="utf-8"),
        )
    )
    assert "name: baseline" in baseline
    for required in (
        "issue_comment:",
        "release:awaiting-agent",
        "release:needs-resume",
        "release:superseded",
        "request-agent",
        "agent_acknowledged",
        "handle-comment",
        "await-ui",
        "deploy-and-verify",
        "complete-standard",
        "halt-merged",
        "resume-halted",
        "retry-blocked",
        "enqueue-loop-new",
        "enqueue-loop-recovery",
        "correct-loop-identity",
        "wb-core-loop-new-root-proof",
        "wb-core-loop-recovery-proof",
        "wb-core-loop-classification-blocker",
        "scope:production-mutation",
        "preflight-production-mutation",
        "/wb-core production-mutation complete",
        "wb-core-production-mutation-completion-proof",
        "--read-only",
        'cron: "*/5 * * * *"',
        "group: wb-core-production-release",
    ):
        assert required in release or required in implementation
    assert release.count("group: wb-core-production-release") == 1
    assert release.count("environment: production") == 3
    assert "reconcile_halted:" in release
    assert "resume-halted" in release


def _monitor_query_matches(query: str, item: dict[str, Any]) -> bool:
    """Evaluate the bounded qualifiers used by the canonical monitor regression."""

    tokens = shlex.split(query)
    if "is:pr" in tokens and item.get("kind") != "pr":
        return False
    if "is:open" in tokens and item.get("state") != "open":
        return False
    if "is:closed" in tokens and item.get("state") != "closed":
        return False

    labels = {str(label) for label in item.get("labels") or []}
    for token in tokens:
        if token.startswith("-label:") and token.removeprefix("-label:") in labels:
            return False
        if token.startswith("label:"):
            alternatives = set(token.removeprefix("label:").split(","))
            if not labels & alternatives:
                return False
    return True


def _assert_codex_task_class_and_monitor_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md").read_text(
        encoding="utf-8"
    )
    release_train = (
        ROOT / "docs" / "architecture" / "11_github_release_train.md"
    ).read_text(encoding="utf-8")

    for source in (agents, execution):
        folded = source.casefold()
        for required in (
            "DISPATCH_REQUEST",
            "discussion-only",
            "запускай/запусти задачу",
            "передавай/отправляй в Codex",
            "начинай/делай/реализуй по этому плану",
            "текущей уже исполняемой Codex-задаче",
            "exact non-terminal target",
            "user-owned Codex task",
            "create_thread",
            "spawn_agent",
            "initiating discussion thread не начинает implementation",
        ):
            assert required.casefold() in folded
        assert "ACTIVE_ADDITION" in source and "ACTIVE_LOOP_RECOVERY" in source
        assert "неоднознач" in folded and "отдельн" in folded
        assert "subagent" in folded and "не являются dispatch" in folded
        assert "ни одно исключение не разрешает `discussion-only` implementation" in folded

    for source in (agents, execution, release_train):
        folded = source.casefold()
        for required in (
            "DISPATCH_REQUEST",
            "launch operation",
            "TARGET_CREATE_READBACK",
            "MONITOR_ATTACH_READBACK",
            "create_thread",
            "spawn_agent",
            "wait_threads(timeoutMs: 0)",
            "fail closed",
            "MONITORING_CAPABILITY_LIMITATION",
        ):
            assert required.casefold() in folded
        assert "target" in folded and "monitor" in folded and "readback" in folded
        for terminal_required in (
            "TERMINAL_MONITOR_SUMMARY",
            "успешно завершена",
            "2–5",
            "Проверено:",
            "canonical terminal state",
            "release:done",
            "release:production",
            "verified user artifact",
            "partial",
            "terminal failure",
            "ложн",
            "только после",
            "silent cleanup",
            "не являются корректным завершением мониторинга",
        ):
            assert terminal_required.casefold() in folded

    explicit_classes = (
        "`КЛАСС ЗАДАЧИ: СТАНДАРТ`",
        "`КЛАСС ЗАДАЧИ: LOOP`",
        "`КЛАСС ЗАДАЧИ: ДИАГНОСТИКА`",
    )
    automatic_messages = (
        "`Класс задачи: стандарт — определён автоматически`",
        "`Класс задачи: loop — определён автоматически`",
        "`Класс задачи: диагностика — определён автоматически`",
    )
    classification_rules = (
        "исключительно read-only анализ без изменений code, GitHub state и production — `диагностика`",
        "deploy с последующими production UI Flow, Playwright-проверками и итерациями до live-результата — `loop`",
        "обычная реализация, repo-only изменение или неоднозначный случай — `стандарт`",
    )
    for source in (agents, execution):
        for required in (*explicit_classes, *automatic_messages, *classification_rules):
            assert required in source
        assert "неоднознач" in source and "всегда" in source and "`стандарт`" in source

    for source in (agents, execution, release_train):
        for required in (
            "NEW_TASK",
            "ACTIVE_ADDITION",
            "ACTIVE_LOOP_RECOVERY",
            "TERMINAL_STALE_REFERENCE",
            "одинаковый чат",
            "release:done",
            "release:production",
            "enqueue-new",
            "enqueue-recovery",
            "correct-to-new",
        ):
            assert required.casefold() in source.casefold()
        assert "root > PR" in source or "root больше номера PR" in source
        assert "retry-blocked" in source and "classification" in source.casefold()

    for source in (agents, release_train):
        assert CANONICAL_MONITOR_URL in source
        assert "apps/github_release_train_spec.py" in source
    for source in (agents, execution, release_train):
        assert "--resume-owner --no-ack-agent" in source
    for source in (agents, execution, release_train):
        assert "deployed <MERGE_SHA> evidence sha256:<EVIDENCE_HASH>" in source
    for required in (
        "task title",
        "class",
        "stage",
        "root",
        "last action",
        "intervention",
        "resume command",
    ):
        assert required in release_train

    assert "## Thread heartbeat automation" in agents
    assert "## Thread Heartbeat Automation" in execution
    assert "## Desktop Thread Heartbeat И Canonical Monitoring" in release_train
    for source in (agents, execution, release_train):
        folded = source.casefold()
        for required in (
            "ровно один",
            "10 минут",
            "exact target",
            "capability",
            "external supervisor",
            "self-heartbeat",
            "mutually-exclusive",
            "не создаёт второй state machine",
            "terminal failure",
            "durable prompt",
            "automation tool",
            "active",
            "external supervisor reporter",
            "self recovery heartbeat",
            "multi-target",
            "reporting intent",
        ):
            assert required.casefold() in folded
        assert "только `external supervisor reporter`" in folded
        assert "`self recovery heartbeat`" in source
        assert "не удовлетворяет reporting intent" in source
        assert "wait_threads(timeoutMs: 0)" in source
        assert "initiating" in folded and "destination" in folded
        assert "non-terminal target" in folded
        assert "successful create-call" in folded and "не completion" in folded
        assert "progress без evidence не начисляется" in folded
        assert "progress weights" in folded
        assert "terminal target" in folded
        assert "FREQ=" not in source
        assert "и только иначе — self-heartbeat" not in source
    for source in (agents, execution):
        assert "Chat → Codex" in source
        assert "создаваемой либо получаемой" in source
        assert "bounded follow-up" in source
        assert "update" in source and "duplicate" in source
        assert "`ACTIVE`" in source
        assert "[<" in source
        assert (
            "Прогресс ≈<процент>% · ETA ≈<диапазон> · "
            "сделано: <одна короткая фраза>."
        ) in source
        assert "ETA ≈зависит от" in source
        assert "компьютер и Desktop должны быть запущены" in source
    for required_example in (
        "Один новый target, свободный initiating thread",
        "Второй параллельный target при active reporter",
        "Reporter unavailable",
        "Terminal cleanup одного из нескольких targets",
    ):
        assert required_example in execution
    assert "сохранив первый non-terminal exact target" in execution
    assert "сохранение остальных targets" in execution
    assert "каждые пять минут" in release_train
    assert "каждые 300 секунд" in release_train
    assert "каждые 10 минут" in release_train
    assert "WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES" in release_train
    assert "если capability недоступна" in agents.casefold()
    assert "если automation capability недоступна" in execution.casefold()
    assert "active target он не будит" in release_train
    assert "read-only наблюдает GitHub" in release_train

    query = parse_qs(urlparse(CANONICAL_MONITOR_URL).query)["q"][0]
    tokens = shlex.split(query)
    assert "is:pr" in tokens
    assert "is:open" not in tokens and "is:closed" not in tokens
    assert "-label:release:superseded" in tokens
    assert "sort:created-asc" in tokens
    label_tokens = [token for token in tokens if token.startswith("label:")]
    assert len(label_tokens) == 1
    assert set(label_tokens[0].removeprefix("label:").split(",")) == MONITORED_RELEASE_LABELS
    assert DONE_LABEL not in MONITORED_RELEASE_LABELS
    assert PRODUCTION_LABEL not in MONITORED_RELEASE_LABELS

    closed_merged_gate = {
        "kind": "pr",
        "state": "closed",
        "merged": True,
        "labels": {AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL},
    }
    assert _monitor_query_matches(query, closed_merged_gate)
    assert not _monitor_query_matches(
        query,
        {**closed_merged_gate, "labels": {AWAITING_UI_LABEL, SUPERSEDED_LABEL}},
    )
    assert not _monitor_query_matches(
        query,
        {"kind": "pr", "state": "closed", "merged": True, "labels": {PRODUCTION_LABEL}},
    )


def _assert_machine_classification_and_state_spec() -> None:
    for line, expected in EXPLICIT_TASK_PROMPTS.items():
        assert explicit_task_class(line + "\nbody") == expected
        assert classify_task(TaskIntent(ambiguous=True), explicit=expected) == expected
    assert classify_task(TaskIntent(read_only=True)) == TaskClass.DIAGNOSTIC
    assert classify_task(TaskIntent(read_only=True, user_artifact=True)) == TaskClass.STANDARD
    assert len(tuple(ExecutionContour)) == 6
    assert not github_closure_required(TaskClass.STANDARD, ExecutionContour.USER_ARTIFACT)
    assert github_closure_required(TaskClass.STANDARD, ExecutionContour.REPO_ONLY)
    assert not github_closure_required(TaskClass.DIAGNOSTIC, ExecutionContour.READ_ONLY)
    assert classify_task(TaskIntent(deploy=True, production_ui=True, iterative=True)) == TaskClass.LOOP
    assert classify_task(TaskIntent()) == TaskClass.STANDARD
    assert classify_task(TaskIntent(ambiguous=True)) == TaskClass.STANDARD
    assert (
        classify_task(TaskIntent(read_only=True), inherited=TaskClass.LOOP)
        == TaskClass.LOOP
    )
    assert ACTIVE_PRIMARY_LABELS.isdisjoint(TERMINAL_LABELS)
    assert ACTIVE_STATE_LABELS == ACTIVE_PRIMARY_LABELS | {NEEDS_RESUME_LABEL}
    assert PRIMARY_STATE_LABELS == ACTIVE_PRIMARY_LABELS | TERMINAL_LABELS
    assert TERMINAL_FORBIDDEN_INHERITANCE == {
        "branch",
        "pr",
        "task_identity",
        "loop_root",
        "acknowledgement",
        "owner_heartbeat",
        "recovery_identity",
    }
    assert NEEDS_RESUME_LABEL in MONITORED_RELEASE_LABELS
    assert DONE_LABEL not in MONITORED_RELEASE_LABELS
    assert PRODUCTION_LABEL not in MONITORED_RELEASE_LABELS
    assert TRANSITION_MATRIX[AWAITING_UI_LABEL] >= {PRODUCTION_LABEL, HALTED_LABEL}
    assert {
        (AWAITING_AGENT_LABEL, READY_LABEL),
        (AWAITING_UI_LABEL, PRODUCTION_LABEL),
        (BLOCKED_LABEL, PRODUCTION_LABEL),
        (HALTED_LABEL, PRODUCTION_LABEL),
    } <= CRITICAL_TRANSITIONS
    all_targets = set(PRIMARY_STATE_LABELS)
    for current, allowed in TRANSITION_MATRIX.items():
        current_labels = set()
        if current == RUNNING_LABEL:
            current_labels = {READY_LABEL, RUNNING_LABEL}
        elif current != "release:none":
            current_labels = {current}
        for target in all_targets:
            if target == current or target in allowed:
                transition_label_set(current_labels, target)
            else:
                try:
                    transition_label_set(current_labels, target)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"forbidden matrix edge accepted: {current} -> {target}")

    diagnostic_api = FakeApi()
    assert classify_task(TaskIntent(read_only=True)) == TaskClass.DIAGNOSTIC
    assert not diagnostic_api.pulls and not diagnostic_api.comments and not diagnostic_api.dispatched


def _assert_resume_status_and_manual_ack_guards() -> None:
    api = FakeApi()
    waiting = _pull(
        90,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(90)],
        created_at="2026-07-20T00:00:00Z",
    )
    waiting["state"] = "closed"
    waiting["merged"] = True
    waiting["merge_commit_sha"] = SHA_A
    api.pulls[90] = waiting
    _add_new_root_proof(api, 90)
    api.add_comment(90, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=90 root=90 -->")
    api.events[90] = [
        {
            "event": "labeled",
            "label": {"name": AWAITING_UI_LABEL},
            "created_at": "2026-07-20T00:00:00Z",
        }
    ]
    now = datetime(2026, 7, 20, 0, 31, tzinfo=timezone.utc).timestamp()
    assert select_candidate(api, needs_resume_after_seconds=1800, now=now)["status"] == "awaiting-ui"
    assert NEEDS_RESUME_LABEL in _labels(waiting)
    status_comments = [body for _, body in api.comments if "wb-core-release-status" in body]
    assert len(status_comments) == 1
    upsert_status_comment(
        api,
        90,
        owner="replacement",
        reason="resume test",
        last_action="heartbeat",
        intervention=False,
        now=now + 1,
    )
    assert len([body for _, body in api.comments if "wb-core-release-status" in body]) == 1
    assert resume_loop_owner(
        api,
        90,
        SHA_A,
        90,
        actor="replacement",
        association="OWNER",
    ) == "resumed"
    assert NEEDS_RESUME_LABEL not in _labels(waiting)
    assert not any(label.startswith(LOOP_ACK_PREFIX) for label in _labels(waiting))
    assert resume_loop_owner(
        api,
        90,
        SHA_A,
        90,
        actor="replacement",
        association="OWNER",
    ) == "resumed"

    forged = _pull(
        91,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_ack_label(SHA_A)],
        created_at="2026-07-20T00:32:00Z",
    )
    forged_api = FakeApi()
    forged_api.pulls[91] = forged
    candidate = _prepare(forged_api, 91)
    assert candidate.agent_acknowledged is False
    try:
        merge_candidate(forged_api, candidate)
    except ReleaseBlocked as exc:
        assert "repo-owned" in str(exc)
    else:
        raise AssertionError("manually forged ack label must not authorize merge")

    terminal_api = FakeApi()
    forged_terminal = _pull(
        92,
        labels=[PRODUCTION_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T00:33:00Z",
    )
    forged_terminal["state"] = "closed"
    forged_terminal["merged"] = True
    forged_terminal["merge_commit_sha"] = SHA_B
    terminal_api.pulls[92] = forged_terminal
    assert wait_for_release(
        terminal_api,
        92,
        status_seconds=0,
        poll_seconds=0,
        acknowledge_agent=False,
        emit=lambda _: None,
    ) == 2

    proven_api = FakeApi()
    proven = _pull(
        93,
        labels=[RUNNING_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T00:34:00Z",
    )
    proven["state"] = "closed"
    proven["merged"] = True
    proven["merge_commit_sha"] = SHA_C
    proven_api.pulls[93] = proven
    complete_standard_release(
        proven_api,
        93,
        merge_sha=SHA_C,
        contour="production-verified",
    )
    assert wait_for_release(
        proven_api,
        93,
        status_seconds=0,
        poll_seconds=0,
        acknowledge_agent=False,
        emit=lambda _: None,
    ) == 0


def _assert_two_parallel_loop_roots() -> None:
    api = FakeApi()
    root_a = _pull(
        100,
        labels=[RUNNING_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T01:00:00Z",
    )
    root_a["state"] = "closed"
    root_a["merged"] = True
    root_a["merge_commit_sha"] = SHA_A
    root_b = _pull(
        101,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T01:01:00Z",
        sha=SHA_C,
    )
    api.pulls = {100: root_a, 101: root_b}
    _add_new_root_proof(api, 100)
    mark_loop_awaiting_ui(api, 100, SHA_A)
    assert select_candidate(api, now=datetime(2026, 7, 20, 1, 2, tzinfo=timezone.utc).timestamp())["status"] == "awaiting-ui"

    recovery = _pull(
        102,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(100)],
        created_at="2026-07-20T01:02:00Z",
        sha=SHA_B,
    )
    api.pulls[102] = recovery
    first = _prepare(api, 102)
    request_loop_agent(api, 102, first.head_sha)
    acknowledge_loop_agent(api, 102, SHA_B, actor="codex", association="OWNER")
    recovery_merge = merge_candidate(api, _prepare(api, 102)).merge_sha
    mark_loop_awaiting_ui(api, 102, recovery_merge)
    assert len(api.list_issues_by_label(AWAITING_UI_LABEL, state="all")) == 1
    try:
        accept_loop_ui(
            api,
            100,
            actor="codex",
            association="OWNER",
            deployed_sha=SHA_A,
            evidence=EVIDENCE,
        )
    except ReleaseBlocked as exc:
        assert "current LOOP iteration" in str(exc)
    else:
        raise AssertionError("stale acceptance of pre-recovery PR must fail")
    accept_loop_ui(
        api,
        102,
        actor="codex",
        association="OWNER",
        deployed_sha=recovery_merge,
        evidence=EVIDENCE,
    )
    selected_b = select_candidate(api, now=datetime(2026, 7, 20, 1, 3, tzinfo=timezone.utc).timestamp())
    assert selected_b["pr_number"] == 101
    first_b = _prepare(api, 101)
    request_loop_agent(api, 101, first_b.head_sha)
    acknowledge_loop_agent(api, 101, SHA_C, actor="codex", association="OWNER")
    merge_b = merge_candidate(api, _prepare(api, 101)).merge_sha
    mark_loop_awaiting_ui(api, 101, merge_b)
    assert len(api.list_issues_by_label(AWAITING_UI_LABEL, state="all")) == 1
    assert api.merges == [(102, SHA_B), (101, SHA_C)]


def _assert_halted_exact_evidence_resume() -> None:
    api = FakeApi()
    halted = _pull(
        110,
        labels=[HALTED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T02:00:00Z",
        sha=SHA_B,
    )
    halted["state"] = "closed"
    halted["merged"] = True
    halted["merge_commit_sha"] = SHA_A
    api.pulls[110] = halted
    evidence = {
        "status": "reconciled",
        "healthy": True,
        "pr": 110,
        "head": SHA_B,
        "merge": SHA_A,
        "expected_sha": SHA_A,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
    }
    assert resume_halted_release(api, 110, evidence) == "production"
    assert PRODUCTION_LABEL in _labels(halted) and HALTED_LABEL not in _labels(halted)
    assert resume_halted_release(api, 110, evidence) == "already-reconciled"
    forged = dict(evidence, expected_sha=SHA_C)
    halted2 = _pull(
        111,
        labels=[HALTED_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T02:01:00Z",
        sha=SHA_B,
    )
    halted2["state"] = "closed"
    halted2["merged"] = True
    halted2["merge_commit_sha"] = SHA_A
    api.pulls[111] = halted2
    try:
        resume_halted_release(api, 111, forged)
    except ReleaseBlocked:
        pass
    else:
        raise AssertionError("wrong exact-SHA evidence must retain release:halted")
    assert HALTED_LABEL in _labels(halted2)

    root = _pull(
        112,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(112)],
        created_at="2026-07-20T02:02:00Z",
        sha=SHA_C,
    )
    root["state"] = "closed"
    root["merged"] = True
    root["merge_commit_sha"] = SHA_C
    api.pulls[112] = root
    _add_new_root_proof(api, 112)
    api.add_comment(
        112,
        f"<!-- wb-core-loop-deploy-proof merge={SHA_C} pr=112 root=112 -->",
    )
    recovery = _pull(
        113,
        labels=[HALTED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(112)],
        created_at="2026-07-20T02:03:00Z",
        sha=SHA_B,
    )
    recovery["state"] = "closed"
    recovery["merged"] = True
    recovery["merge_commit_sha"] = SHA_A
    api.pulls[113] = recovery
    api.add_comment(
        113,
        f"<!-- wb-core-loop-recovery-proof gate=112 head={SHA_B} pr=113 root=112 -->",
    )
    recovery_evidence = {
        "status": "reconciled",
        "healthy": True,
        "pr": 113,
        "head": SHA_B,
        "merge": SHA_A,
        "expected_sha": SHA_A,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
    }
    assert resume_halted_release(api, 113, recovery_evidence) == "superseded-iteration"
    assert HALTED_LABEL not in _labels(recovery)
    assert AWAITING_UI_LABEL in _labels(root)


def _terminal_loop_fixture(api: FakeApi, number: int, *, merge_sha: str = SHA_A) -> dict[str, Any]:
    pull = _pull(
        number,
        labels=[PRODUCTION_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(number)],
        created_at="2026-07-20T03:00:00Z",
    )
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merge_commit_sha"] = merge_sha
    api.pulls[number] = pull
    api.add_comment(
        number,
        f"<!-- wb-core-loop-chain-audit merge={merge_sha} root={number} terminal_pr={number} -->",
    )
    return pull


def _active_loop_gate_fixture(api: FakeApi, number: int) -> dict[str, Any]:
    pull = _pull(
        number,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(number)],
        created_at="2026-07-20T03:01:00Z",
    )
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merge_commit_sha"] = SHA_A
    api.pulls[number] = pull
    _add_new_root_proof(api, number)
    api.add_comment(
        number,
        f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr={number} root={number} -->",
    )
    return pull


def _assert_continuity_classification_matrix() -> None:
    """Twenty named regressions for task continuity and LOOP registration identity."""

    completed: list[str] = []

    assert classify_continuity(
        ContinuityIntent(explicit_addition=True, referenced_release_state=READY_LABEL)
    ) == TaskContinuity.ACTIVE_ADDITION
    completed.append("1_active_addition_inherits")

    assert classify_continuity(
        ContinuityIntent(explicit_addition=True, referenced_release_state=DONE_LABEL)
    ) == TaskContinuity.NEW_TASK
    completed.append("2_done_addition_is_new")

    assert classify_continuity(
        ContinuityIntent(explicit_addition=True, referenced_release_state=PRODUCTION_LABEL)
    ) == TaskContinuity.NEW_TASK
    completed.append("3_production_addition_is_new")

    assert classify_continuity(
        ContinuityIntent(
            explicit_addition=True,
            referenced_release_state=PRODUCTION_LABEL,
            same_chat=True,
        )
    ) == TaskContinuity.NEW_TASK
    completed.append("4_same_chat_terminal_is_new")

    assert classify_continuity(
        ContinuityIntent(
            referenced_release_state=PRODUCTION_LABEL,
            same_functional_area=True,
        ),
        task_class=TaskClass.LOOP,
    ) == TaskContinuity.NEW_TASK
    completed.append("5_same_area_new_defect_is_new")

    assert classify_continuity(
        ContinuityIntent(prompt="Новая отдельная LOOP-задача для того же экрана"),
        task_class=TaskClass.LOOP,
    ) == TaskContinuity.NEW_TASK
    completed.append("6_explicit_new_loop_forces_new")

    assert classify_continuity(
        ContinuityIntent(
            explicit_recovery=True,
            referenced_release_state=AWAITING_UI_LABEL,
            defect_found_during_active_ui=True,
        ),
        task_class=TaskClass.LOOP,
    ) == TaskContinuity.ACTIVE_LOOP_RECOVERY
    completed.append("7_active_ui_recovery_inherits")

    assert classify_continuity(
        ContinuityIntent(
            explicit_recovery=True,
            referenced_release_state=PRODUCTION_LABEL,
            defect_found_during_active_ui=True,
        ),
        task_class=TaskClass.LOOP,
    ) == TaskContinuity.TERMINAL_STALE_REFERENCE
    completed.append("8_terminal_recovery_reference_rejected")

    foreign_api = FakeApi()
    _active_loop_gate_fixture(foreign_api, 300)
    foreign_api.pulls[301] = _pull(
        301,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T03:02:00Z",
        sha=SHA_B,
    )
    foreign_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    enqueue_loop_new(
        foreign_api, 301, SHA_B, actor="codex", association="OWNER"
    )
    waiting = select_candidate(foreign_api)
    assert waiting["status"] == "awaiting-ui" and READY_LABEL in _labels(foreign_api.pulls[301])
    completed.append("9_new_loop_foreign_gate_waits")

    serial_api = FakeApi()
    for number, sha in ((310, SHA_A), (311, SHA_B)):
        serial_api.pulls[number] = _pull(
            number,
            labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
            created_at=f"2026-07-20T03:{number - 300:02d}:00Z",
            sha=sha,
        )
    serial_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    enqueue_loop_new(serial_api, 310, SHA_A, actor="codex", association="OWNER")
    enqueue_loop_new(serial_api, 311, SHA_B, actor="codex", association="OWNER")
    assert select_candidate(serial_api)["pr_number"] == 310
    request_loop_agent(serial_api, 310, _prepare(serial_api, 310).head_sha)
    assert select_candidate(serial_api)["status"] == "awaiting-agent"
    assert READY_LABEL in _labels(serial_api.pulls[311])
    completed.append("10_independent_roots_serialize")

    recovery_api = FakeApi()
    _active_loop_gate_fixture(recovery_api, 320)
    recovery_api.pulls[321] = _pull(
        321,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T03:21:00Z",
        sha=SHA_B,
    )
    recovery_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    enqueue_loop_recovery(
        recovery_api,
        321,
        SHA_B,
        gate_pr=320,
        expected_root=320,
        actor="codex",
        association="OWNER",
    )
    assert select_candidate(recovery_api)["pr_number"] == 321
    assert loop_registration_kind(recovery_api, recovery_api.pulls[321]) == "recovery"
    completed.append("11_same_root_recovery_allowed")

    future_api = FakeApi()
    future_api.pulls[330] = _pull(
        330,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(331)],
        created_at="2026-07-20T03:30:00Z",
    )
    try:
        loop_registration_kind(future_api, future_api.pulls[330])
    except ReleaseClassificationBlocked as exc:
        assert exc.code == "loop-root-future"
    else:
        raise AssertionError("root greater than PR must be rejected")
    completed.append("12_future_root_rejected")

    manual_api = FakeApi()
    manual_api.pulls[331] = _pull(
        331,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(331)],
        created_at="2026-07-20T03:31:00Z",
    )
    try:
        loop_registration_kind(manual_api, manual_api.pulls[331])
    except ReleaseClassificationBlocked as exc:
        assert exc.code == "loop-new-proof-missing"
        mark_classification_blocked(manual_api, 331, exc)
    else:
        raise AssertionError("manual root label without proof must be rejected")
    assert BLOCKED_LABEL in _labels(manual_api.pulls[331])
    assert any(
        "classification error `loop-new-proof-missing`" in body
        and "wb-core-release-status" in body
        and "retry-blocked` is forbidden" not in body
        and "--resume-owner" not in body
        for _, body in manual_api.comments
    )
    completed.append("13_manual_root_without_proof_rejected")

    terminal_api = FakeApi()
    _terminal_loop_fixture(terminal_api, 340)
    terminal_api.pulls[341] = _pull(
        341,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T03:41:00Z",
        sha=SHA_B,
    )
    terminal_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    try:
        enqueue_loop_recovery(
            terminal_api,
            341,
            SHA_B,
            gate_pr=340,
            expected_root=340,
            actor="codex",
            association="OWNER",
        )
    except ReleaseClassificationBlocked as exc:
        assert exc.code == "loop-recovery-root-terminal"
    else:
        raise AssertionError("terminal root must not reactivate")
    completed.append("14_terminal_root_cannot_reactivate")

    retry_api = FakeApi()
    retry_api.pulls[400] = _pull(
        400,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(400)],
        created_at="2026-07-20T04:00:00Z",
    )
    _add_new_root_proof(retry_api, 400)
    retry_api.pulls[400]["head"]["sha"] = SHA_B
    retry_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    identity_before = _labels(retry_api.pulls[400]) - {BLOCKED_LABEL}
    try:
        enqueue_loop_new(retry_api, 400, SHA_B, actor="codex", association="OWNER")
    except ReleaseBlocked as exc:
        assert "retry-blocked" in str(exc)
    else:
        raise AssertionError("technical blocker must not be cleared by LOOP enrollment")
    retry_blocked_release(retry_api, 400, expected_head_sha=SHA_B, check_name="baseline")
    identity_after = _labels(retry_api.pulls[400]) - {READY_LABEL}
    assert identity_before == identity_after
    assert any(
        f"wb-core-loop-new-root-proof head={SHA_B} pr=400 root=400" in body
        for _, body in retry_api.comments
    )
    set_release_state(retry_api, 400, BLOCKED_LABEL)
    retry_api.pulls[400]["head"]["sha"] = SHA_C
    assert handle_loop_comment(
        retry_api,
        400,
        f"/wb-core loop retry-blocked 400 head {SHA_C}",
        actor="codex",
        association="OWNER",
    ) == READY_LABEL
    assert any(
        f"wb-core-loop-new-root-proof head={SHA_C} pr=400 root=400" in body
        for _, body in retry_api.comments
    )
    set_release_state(retry_api, 400, BLOCKED_LABEL)
    try:
        handle_loop_comment(
            retry_api,
            400,
            f"/wb-core loop retry-blocked 400 head {SHA_C}",
            actor="outside-user",
            association="NONE",
        )
    except ReleaseBlocked as exc:
        assert "write association" in str(exc)
    else:
        raise AssertionError("trusted retry command must reject an unprivileged actor")
    standard_retry_api = FakeApi()
    standard_retry_api.pulls[402] = _pull(
        402,
        labels=[BLOCKED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-20T04:02:00Z",
        sha=SHA_C,
    )
    standard_retry_api.checks = list(retry_api.checks)
    try:
        handle_loop_comment(
            standard_retry_api,
            402,
            f"/wb-core loop retry-blocked 402 head {SHA_C}",
            actor="codex",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "task:loop" in str(exc)
    else:
        raise AssertionError("LOOP retry command must reject a standard PR")
    set_release_state(retry_api, 400, READY_LABEL)
    completed.append("15_generic_retry_preserves_classification")

    classification_api = FakeApi()
    classification_api.pulls[401] = _pull(
        401,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(401)],
        created_at="2026-07-20T04:01:00Z",
    )
    classification_api.add_comment(
        401,
        f"<!-- wb-core-loop-classification-blocker head={SHA_A} pr=401 -->",
    )
    classification_api.pulls[401]["head"]["sha"] = SHA_B
    classification_api.checks = list(retry_api.checks)
    try:
        retry_blocked_release(
            classification_api, 401, expected_head_sha=SHA_B, check_name="baseline"
        )
    except ReleaseClassificationBlocked as exc:
        assert exc.code == "generic-retry-classification-forbidden"
    else:
        raise AssertionError("classification blocker must reject generic retry")
    assert enqueue_loop_new(
        classification_api, 401, SHA_B, actor="codex", association="OWNER"
    ) == "enqueued-new"
    _set_labels(
        classification_api.pulls[401],
        _labels(classification_api.pulls[401]) - {loop_root_label(401)},
    )
    mark_classification_blocked(
        classification_api,
        401,
        ReleaseClassificationBlocked("loop-root-missing", "registered root disappeared"),
    )
    try:
        retry_blocked_release(
            classification_api, 401, expected_head_sha=SHA_B, check_name="baseline"
        )
    except ReleaseClassificationBlocked as exc:
        assert exc.code == "generic-retry-classification-forbidden"
    else:
        raise AssertionError("a later same-head classification blocker must remain unresolved")
    assert enqueue_loop_new(
        classification_api, 401, SHA_B, actor="codex", association="OWNER"
    ) == "enqueued-new"
    set_release_state(classification_api, 401, BLOCKED_LABEL)
    classification_api.pulls[401]["head"]["sha"] = SHA_C
    assert retry_blocked_release(
        classification_api, 401, expected_head_sha=SHA_C, check_name="baseline"
    ) == READY_LABEL
    completed.append("16_generic_retry_rejects_classification")

    correction_api = FakeApi()
    _terminal_loop_fixture(correction_api, 350)
    _terminal_loop_fixture(correction_api, 360, merge_sha=SHA_C)
    correction_api.pulls[351] = _pull(
        351,
        labels=[BLOCKED_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(350)],
        created_at="2026-07-20T04:11:00Z",
        sha=SHA_B,
    )
    correction_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    try:
        correct_loop_identity_to_new(
            correction_api,
            351,
            SHA_B,
            expected_old_root=350,
            actor="codex",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "release:blocked" in str(exc) or "classification" in str(exc)
    else:
        raise AssertionError("correction without exact classification evidence must fail")
    completed.append("17_correction_requires_exact_evidence")

    correction_api.add_comment(
        351,
        f"<!-- wb-core-loop-classification-blocker head={SHA_B} pr=351 -->",
    )
    other_root_snapshot = (
        set(_labels(correction_api.pulls[360])),
        list(correction_api.list_comments(360)),
    )
    assert correct_loop_identity_to_new(
        correction_api,
        351,
        SHA_B,
        expected_old_root=350,
        actor="codex",
        association="OWNER",
    ) == "corrected-to-new"
    _set_labels(
        correction_api.pulls[351],
        (_labels(correction_api.pulls[351]) - {loop_root_label(351)})
        | {loop_root_label(350)},
    )
    mark_classification_blocked(
        correction_api,
        351,
        ReleaseClassificationBlocked("loop-root-stale", "old terminal root returned"),
    )
    assert correct_loop_identity_to_new(
        correction_api,
        351,
        SHA_B,
        expected_old_root=350,
        actor="codex",
        association="OWNER",
    ) == "corrected-to-new"
    set_release_state(correction_api, 351, BLOCKED_LABEL)
    assert retry_blocked_release(
        correction_api, 351, expected_head_sha=SHA_B, check_name="baseline"
    ) == READY_LABEL
    correction_comment_count = len(correction_api.list_comments(351))
    set_release_state(correction_api, 351, RUNNING_LABEL)
    correction_state = set(_labels(correction_api.pulls[351]))
    correction_dispatches = list(correction_api.dispatched)
    assert correct_loop_identity_to_new(
        correction_api,
        351,
        SHA_B,
        expected_old_root=350,
        actor="codex",
        association="OWNER",
    ) == "already-corrected-to-new"
    assert len(correction_api.list_comments(351)) == correction_comment_count
    assert _labels(correction_api.pulls[351]) == correction_state
    assert correction_api.dispatched == correction_dispatches

    enqueue_api = FakeApi()
    enqueue_api.pulls[500] = _pull(
        500,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T05:00:00Z",
    )
    enqueue_api.checks = list(correction_api.checks)
    assert enqueue_loop_new(
        enqueue_api, 500, SHA_A, actor="codex", association="OWNER"
    ) == "enqueued-new"
    proof_comment_count = len(enqueue_api.list_comments(500))
    set_release_state(enqueue_api, 500, RUNNING_LABEL)
    enqueue_state = set(_labels(enqueue_api.pulls[500]))
    enqueue_dispatches = list(enqueue_api.dispatched)
    assert enqueue_loop_new(
        enqueue_api, 500, SHA_A, actor="codex", association="OWNER"
    ) == "already-enqueued-new"
    assert len(enqueue_api.list_comments(500)) == proof_comment_count
    assert _labels(enqueue_api.pulls[500]) == enqueue_state
    assert enqueue_api.dispatched == enqueue_dispatches
    completed.append("18_enqueue_and_correction_idempotent")

    assert (
        set(_labels(correction_api.pulls[360])),
        list(correction_api.list_comments(360)),
    ) == other_root_snapshot
    completed.append("19_other_roots_unchanged")

    incident_api = FakeApi()
    root_650 = _pull(
        650,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(650)],
        created_at="2026-07-20T06:10:00Z",
    )
    root_650.update(state="closed", merged=True, merge_commit_sha=SHA_A)
    terminal_667 = _pull(
        667,
        labels=[PRODUCTION_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(650)],
        created_at="2026-07-20T06:11:00Z",
        sha=SHA_B,
    )
    terminal_667.update(state="closed", merged=True, merge_commit_sha=SHA_C)
    incident_api.pulls = {650: root_650, 667: terminal_667}
    _add_new_root_proof(incident_api, 650)
    incident_api.add_comment(
        650, f"<!-- wb-core-loop-deploy-proof merge={SHA_A} pr=650 root=650 -->"
    )
    incident_api.add_comment(
        667,
        f"<!-- wb-core-loop-recovery-proof gate=650 head={SHA_B} pr=667 root=650 -->",
    )
    incident_api.add_comment(
        667,
        f"<!-- wb-core-loop-chain-audit merge={SHA_C} root=650 terminal_pr=667 -->",
    )
    old_chain_snapshot = {
        number: (set(_labels(incident_api.pulls[number])), list(incident_api.list_comments(number)))
        for number in (650, 667)
    }
    incident_api.pulls[674] = _pull(
        674,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-20T06:14:00Z",
        sha=SHA_C,
    )
    incident_api.checks = list(correction_api.checks)
    continuity = classify_continuity(
        ContinuityIntent(
            prompt="Новая LOOP-задача в том же функциональном разделе",
            referenced_release_state=PRODUCTION_LABEL,
            same_chat=True,
            same_functional_area=True,
        ),
        task_class=TaskClass.LOOP,
    )
    assert continuity == TaskContinuity.NEW_TASK
    enqueue_loop_new(incident_api, 674, SHA_C, actor="codex", association="OWNER")
    assert loop_root_from_labels(_labels(incident_api.pulls[674])) == 674
    assert loop_registration_kind(incident_api, incident_api.pulls[674]) == "new"
    assert {
        number: (set(_labels(incident_api.pulls[number])), list(incident_api.list_comments(number)))
        for number in (650, 667)
    } == old_chain_snapshot
    completed.append("20_incident_650_667_674_is_new_root_674")

    assert len(completed) == 20, completed
    print(f"continuity_classification_matrix: {len(completed)}/20 ok")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuity-only", action="store_true")
    parser.add_argument("--goal-only", action="store_true")
    args = parser.parse_args()
    if args.continuity_only:
        _assert_continuity_classification_matrix()
        return 0
    if args.goal_only:
        _assert_goal_shepherd_regressions()
        _assert_phase_local_goal_regressions()
        return 0
    _assert_label_and_input_validation()
    _assert_standard_repo_only_and_live()
    api, root = _assert_loop_handshake_and_gate()
    _assert_recovery_transfer_and_acceptance(api, root)
    _assert_foreign_gate_waiting_and_queue_progress()
    _assert_lost_owner_resume_lifecycle()
    _assert_superseded_normalization_is_root_bounded()
    _assert_blocked_halted_and_production_mutation()
    _assert_production_mutation_terminalization()
    _assert_ack_invalidated_by_head_change()
    _assert_waiter_contract()
    _assert_goal_shepherd_regressions()
    _assert_phase_local_goal_regressions()
    _assert_workflow_contract()
    _assert_codex_task_class_and_monitor_contract()
    _assert_machine_classification_and_state_spec()
    _assert_resume_status_and_manual_ack_guards()
    _assert_two_parallel_loop_roots()
    _assert_halted_exact_evidence_resume()
    _assert_continuity_classification_matrix()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
