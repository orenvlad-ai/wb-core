"""Deterministic state-machine coverage for the GitHub Release Train."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Iterable


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
    PRODUCTION_LABEL,
    PRODUCTION_MUTATION_LABEL,
    READY_LABEL,
    REPO_ONLY_LABEL,
    RUNNING_LABEL,
    STANDARD_TASK_LABEL,
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
    EXIT_TIMEOUT,
    evaluate_release,
    wait_for_release,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


class FakeApi:
    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comparisons: list[dict[str, Any]] = [{"behind_by": 0}]
        self.checks: list[dict[str, Any]] = []
        self.updated: list[tuple[int, str]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.ensured_labels: list[str] = []
        self.comments: list[tuple[int, str]] = []
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
        current.update(str(label) for label in labels)
        _set_labels(self.pulls[number], current)

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
    api = FakeApi()
    loop = _pull(
        40,
        labels=[AWAITING_AGENT_LABEL, LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-17T09:00:00Z",
    )
    api.pulls[40] = loop
    output: list[str] = []
    code = wait_for_release(
        api,
        40,
        timeout_seconds=0,
        poll_seconds=0,
        acknowledge_agent=True,
        emit=output.append,
    )
    assert code == EXIT_TIMEOUT
    assert api.comments[-1] == (40, f"/wb-core loop ack-agent 40 head {SHA_A}")
    set_release_state(api, 40, AWAITING_UI_LABEL)
    assert (
        wait_for_release(
            api,
            40,
            timeout_seconds=0,
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
        "request-agent",
        "agent_acknowledged",
        "handle-comment",
        "await-ui",
        "deploy-and-verify",
        "scope:production-mutation",
    ):
        assert required in release or required in (ROOT / "apps" / "github_release_train.py").read_text(
            encoding="utf-8"
        )


def main() -> int:
    _assert_label_and_input_validation()
    _assert_standard_repo_only_and_live()
    api, root = _assert_loop_handshake_and_gate()
    _assert_recovery_transfer_and_acceptance(api, root)
    _assert_blocked_halted_and_production_mutation()
    _assert_ack_invalidated_by_head_change()
    _assert_waiter_contract()
    _assert_workflow_contract()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
