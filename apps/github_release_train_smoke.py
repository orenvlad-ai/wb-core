"""Deterministic state-machine coverage for the GitHub Release Train."""

from __future__ import annotations

from datetime import datetime, timezone
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
    accept_loop_ui,
    acknowledge_loop_agent,
    loop_ack_label,
    loop_root_label,
    mark_loop_awaiting_ui,
    merge_candidate,
    prepare_candidate,
    request_loop_agent,
    require_deploy_environment,
    scope_from_labels,
    select_candidate,
    set_release_state,
    task_class_from_labels,
    transition_label_set,
)
from apps.github_release_train_wait import (  # noqa: E402
    EXIT_AWAITING_UI,
    evaluate_release,
    wait_for_release,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
CANONICAL_MONITOR_URL = (
    "https://github.com/orenvlad-ai/wb-core/pulls?"
    "q=is%3Apr+-label%3Arelease%3Asuperseded+"
    "label%3A%22release%3Aready%2Crelease%3Arunning%2C"
    "release%3Aawaiting-agent%2Crelease%3Aawaiting-ui%2C"
    "release%3Aneeds-resume%2Crelease%3Ablocked%2Crelease%3Ahalted%22+"
    "sort%3Acreated-asc"
)
MONITORED_RELEASE_LABELS = {
    READY_LABEL,
    RUNNING_LABEL,
    AWAITING_AGENT_LABEL,
    AWAITING_UI_LABEL,
    NEEDS_RESUME_LABEL,
    BLOCKED_LABEL,
    HALTED_LABEL,
}


class FakeApi:
    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comparisons: list[dict[str, Any]] = [{"behind_by": 0}]
        self.checks: list[dict[str, Any]] = []
        self.updated: list[tuple[int, str]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.ensured_labels: list[str] = []
        self.comments: list[tuple[int, str]] = []
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
        return [{"body": body} for comment_number, body in self.comments if comment_number == number]

    def get_pull(self, number: int) -> dict[str, Any]:
        return self.pulls[number]

    def compare(self, base: str, head: str) -> dict[str, Any]:
        assert base == "main"
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

    def remove_label(self, number: int, label: str) -> None:
        current = _labels(self.pulls[number])
        current.discard(label)
        _set_labels(self.pulls[number], current)

    def add_comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)


def _labels(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in payload.get("labels") or []
    }


def _set_labels(payload: dict[str, Any], labels: Iterable[str]) -> None:
    payload["labels"] = [{"name": label} for label in sorted(set(labels))]


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
    return prepare_candidate(
        api,
        "orenvlad-ai/wb-core",
        number,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )


def _assert_label_and_input_validation() -> None:
    running = transition_label_set(
        {READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL, BLOCKED_LABEL},
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
    waiting = select_candidate(api)
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
    assert accept_loop_ui(api, 12, actor="codex", association="OWNER") == "accepted"
    assert PRODUCTION_LABEL in _labels(root)
    assert PRODUCTION_LABEL in _labels(recovery)
    assert select_candidate(api)["pr_number"] == 11
    assert accept_loop_ui(api, 12, actor="codex", association="OWNER") == "already-accepted"
    assert len(api.dispatched) == dispatched_before + 2
    assert PRODUCTION_LABEL in _labels(root) and PRODUCTION_LABEL in _labels(recovery)
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
    loop_b = _pull(
        51,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T05:31:00Z",
        sha=SHA_B,
    )
    api.pulls = {50: loop_a, 51: loop_b}

    waiting = select_candidate(api)
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
    assert len(api.comments) == 1
    assert "python3 apps/github_release_train_wait.py 60" in api.comments[0][1]
    assert SHA_A in api.comments[0][1]
    second = select_candidate(api, needs_resume_after_seconds=1800, now=observed_at + 300)
    assert second["status"] == "awaiting-agent" and second["needs_resume"]
    assert len(api.comments) == 1
    assert READY_LABEL in _labels(queued)

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

    assert accept_loop_ui(api, 73, actor="codex", association="OWNER") == "accepted"
    assert PRODUCTION_LABEL in _labels(root)
    assert PRODUCTION_LABEL in _labels(accepted)
    assert SUPERSEDED_LABEL in _labels(failed)
    assert BLOCKED_LABEL not in _labels(failed)
    assert loop_root_label(70) in _labels(failed)
    assert BLOCKED_LABEL in _labels(other_root)
    assert SUPERSEDED_LABEL not in _labels(other_root)


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


def _assert_ack_invalidated_by_head_change() -> None:
    api = FakeApi()
    loop = _pull(
        30,
        labels=[AWAITING_AGENT_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T08:00:00Z",
    )
    api.pulls[30] = loop
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
                        transition_label_set(_labels(self.pulls[81]), AWAITING_AGENT_LABEL),
                    )
                    self.foreign_released = True
            return super().get_pull(number)

        def add_comment(self, number: int, body: str) -> None:
            super().add_comment(number, body)
            if number == 81 and body.startswith("/wb-core loop ack-agent"):
                _set_labels(
                    self.pulls[81],
                    transition_label_set(_labels(self.pulls[81]), PRODUCTION_LABEL),
                )

    api = AdvancingWaitApi()
    foreign = _pull(
        80,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(80)],
        created_at="2026-07-17T09:00:00Z",
    )
    foreign["state"] = "closed"
    foreign["merged"] = True
    loop = _pull(
        81,
        labels=[READY_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T09:01:00Z",
    )
    api.pulls = {80: foreign, 81: loop}
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
    assert api.comments[-1] == (81, f"/wb-core loop ack-agent 81 head {SHA_A}")
    assert any("normal queue waiting continues" in line for line in output)
    assert not any("fail-closed" in line for line in output)

    ui_api = FakeApi()
    ui_loop = _pull(
        82,
        labels=[AWAITING_UI_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL, loop_root_label(82)],
        created_at="2026-07-17T09:02:00Z",
    )
    ui_api.pulls[82] = ui_loop
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


def _assert_workflow_contract() -> None:
    baseline = (ROOT / ".github" / "workflows" / "baseline-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release-train.yml").read_text(encoding="utf-8")
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
        "scope:production-mutation",
        'cron: "*/5 * * * *"',
        "group: wb-core-production-release",
    ):
        assert required in release or required in (ROOT / "apps" / "github_release_train.py").read_text(
            encoding="utf-8"
        )
    assert release.count("group: wb-core-production-release") == 1
    assert release.count("environment: production") == 1


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

    explicit_classes = (
        "`Класс задачи: стандарт`",
        "`Класс задачи: loop`",
        "`Класс задачи: диагностика`",
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

    for required in (
        "не создаёт новую задачу или PR",
        "текущей ветке",
        "не меняет класс молча",
        "только по прямому указанию пользователя",
    ):
        assert required in agents
    for source in (execution, release_train):
        assert "наследует" in source
        assert "не меняет класс молча" in source
        assert "только по прямому указанию пользователя" in source

    for source in (agents, release_train):
        assert CANONICAL_MONITOR_URL in source

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


def main() -> int:
    _assert_label_and_input_validation()
    _assert_standard_repo_only_and_live()
    api, root = _assert_loop_handshake_and_gate()
    _assert_recovery_transfer_and_acceptance(api, root)
    _assert_foreign_gate_waiting_and_queue_progress()
    _assert_lost_owner_resume_lifecycle()
    _assert_superseded_normalization_is_root_bounded()
    _assert_blocked_halted_and_production_mutation()
    _assert_ack_invalidated_by_head_change()
    _assert_waiter_contract()
    _assert_workflow_contract()
    _assert_codex_task_class_and_monitor_contract()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
