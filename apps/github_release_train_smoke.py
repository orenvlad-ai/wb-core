"""Deterministic smoke coverage for the GitHub release train."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_train import (  # noqa: E402
    BLOCKED_LABEL,
    Candidate,
    DONE_LABEL,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    PRODUCTION_LABEL,
    PRODUCTION_MUTATION_LABEL,
    READY_LABEL,
    REPO_ONLY_LABEL,
    RUNNING_LABEL,
    ReleaseBlocked,
    merge_candidate,
    prepare_candidate,
    require_deploy_environment,
    scope_from_labels,
    select_candidate,
    transition_label_set,
    wait_for_required_check,
)


class FakeApi:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comparisons: list[dict[str, Any]] = [{"behind_by": 0}]
        self.checks: list[dict[str, Any]] = []
        self.updated: list[tuple[int, str]] = []
        self.dispatched: list[tuple[str, str]] = []
        self.added_labels: list[tuple[int, tuple[str, ...]]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.merges: list[tuple[int, str]] = []
        self.mutate_head_on_dispatch = False

    def ensure_label(self, name: str, color: str, description: str) -> None:
        raise AssertionError("not used")

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.issues
            if label in _labels(item) and (state == "all" or item.get("state") == state)
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
        self.pulls[number]["head"]["sha"] = "updated-sha"

    def dispatch_workflow(self, workflow: str, ref: str) -> None:
        self.dispatched.append((workflow, ref))
        if self.mutate_head_on_dispatch:
            for pull in self.pulls.values():
                pull["head"]["sha"] = "unchecked-race-sha"
        next_id = max((int(item.get("id") or 0) for item in self.checks), default=0) + 1
        self.checks.append(
            {"id": next_id, "name": "baseline", "status": "completed", "conclusion": "success"}
        )

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        return list(self.checks)

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]:
        self.merges.append((number, expected_head_sha))
        return {"merged": True, "sha": "merge-sha"}

    def add_labels(self, number: int, labels: Iterable[str]) -> None:
        self.added_labels.append((number, tuple(sorted(labels))))

    def remove_label(self, number: int, label: str) -> None:
        self.removed_labels.append((number, label))

    def add_comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def delete_branch(self, branch: str) -> None:
        raise AssertionError("not used")


def _labels(payload: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in payload.get("labels") or []
    }


def _pull(number: int, *, labels: list[str], created_at: str, sha: str = "head-sha") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
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


def _assert_label_state_machine() -> None:
    current = {READY_LABEL, REPO_ONLY_LABEL, BLOCKED_LABEL}
    running = transition_label_set(current, RUNNING_LABEL)
    assert running == {READY_LABEL, RUNNING_LABEL, REPO_ONLY_LABEL}
    production = transition_label_set(running, PRODUCTION_LABEL)
    assert production == {PRODUCTION_LABEL, REPO_ONLY_LABEL}
    done = transition_label_set({READY_LABEL, LIVE_RUNTIME_LABEL}, DONE_LABEL)
    assert done == {DONE_LABEL, LIVE_RUNTIME_LABEL}


def _assert_scope_is_exact() -> None:
    assert scope_from_labels({READY_LABEL, REPO_ONLY_LABEL}) == REPO_ONLY_LABEL
    for labels in ({READY_LABEL}, {REPO_ONLY_LABEL, LIVE_RUNTIME_LABEL}):
        try:
            scope_from_labels(labels)
        except ReleaseBlocked:
            pass
        else:
            raise AssertionError(f"ambiguous scope must block: {labels}")


def _assert_selection_and_halt() -> None:
    api = FakeApi()
    blocked = _pull(1, labels=[READY_LABEL, BLOCKED_LABEL, REPO_ONLY_LABEL], created_at="2026-07-15T01:00:00Z")
    later = _pull(2, labels=[READY_LABEL, LIVE_RUNTIME_LABEL], created_at="2026-07-15T03:00:00Z")
    earlier = _pull(3, labels=[READY_LABEL, REPO_ONLY_LABEL], created_at="2026-07-15T02:00:00Z")
    api.issues = [blocked, later, earlier]
    api.pulls = {1: blocked, 2: later, 3: earlier}
    selected = select_candidate(api)
    assert selected["pr_number"] == 3
    assert selected["scope"] == REPO_ONLY_LABEL

    halted = _pull(4, labels=[HALTED_LABEL, LIVE_RUNTIME_LABEL], created_at="2026-07-14T01:00:00Z")
    halted["state"] = "closed"
    api.issues.append(halted)
    api.pulls[4] = halted
    result = select_candidate(api)
    assert result == {"status": "halted", "found": False, "halted_pr_number": 4}


def _assert_ci_gate_and_branch_update() -> None:
    api = FakeApi()
    pull = _pull(7, labels=[READY_LABEL, LIVE_RUNTIME_LABEL], created_at="2026-07-15T04:00:00Z")
    api.pulls[7] = pull
    api.comparisons = [{"behind_by": 3}, {"behind_by": 0}]
    api.checks = [{"id": 8, "name": "baseline", "status": "completed", "conclusion": "success"}]
    candidate = prepare_candidate(
        api,
        "orenvlad-ai/wb-core",
        7,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )
    assert candidate.head_sha == "updated-sha"
    assert candidate.deploy_required is True
    assert api.updated == [(7, "head-sha")]
    assert api.dispatched == [("baseline-ci.yml", "feature/7")]

    api.checks = [{"id": 10, "name": "baseline", "status": "completed", "conclusion": "failure"}]
    try:
        wait_for_required_check(api, "updated-sha", "baseline", timeout_seconds=1, poll_seconds=0)
    except ReleaseBlocked as exc:
        assert "failure" in str(exc)
    else:
        raise AssertionError("failed baseline check must block release")

    raced = FakeApi()
    raced.pulls[9] = _pull(
        9,
        labels=[READY_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-15T04:30:00Z",
    )
    raced.mutate_head_on_dispatch = True
    try:
        prepare_candidate(
            raced,
            "orenvlad-ai/wb-core",
            9,
            check_name="baseline",
            timeout_seconds=1,
            poll_seconds=0,
        )
    except ReleaseBlocked as exc:
        assert "head changed" in str(exc)
    else:
        raise AssertionError("an unchecked head change during CI must block release")


def _assert_mutation_and_missing_secret_block() -> None:
    api = FakeApi()
    api.pulls[8] = _pull(
        8,
        labels=[READY_LABEL, PRODUCTION_MUTATION_LABEL],
        created_at="2026-07-15T05:00:00Z",
    )
    try:
        prepare_candidate(
            api,
            "orenvlad-ai/wb-core",
            8,
            check_name="baseline",
            timeout_seconds=1,
            poll_seconds=0,
        )
    except ReleaseBlocked as exc:
        assert "human gate" in str(exc)
    else:
        raise AssertionError("production mutation must be blocked")

    try:
        require_deploy_environment({})
    except ReleaseBlocked as exc:
        assert "production secrets" in str(exc)
    else:
        raise AssertionError("missing production secrets must block live release")
    require_deploy_environment(
        {
            "WB_CORE_DEPLOY_SSH_KEY": "private-key",
            "WB_CORE_DEPLOY_KNOWN_HOSTS": "known-host",
        }
    )


def _assert_exact_merge_gate() -> None:
    api = FakeApi()
    pull = _pull(10, labels=[READY_LABEL, RUNNING_LABEL, REPO_ONLY_LABEL], created_at="2026-07-15T06:00:00Z")
    api.pulls[10] = pull
    candidate = Candidate(
        number=10,
        title="PR 10",
        head_sha="head-sha",
        head_ref="feature/10",
        scope=REPO_ONLY_LABEL,
    )
    assert merge_candidate(api, candidate) == "merge-sha"
    assert api.merges == [(10, "head-sha")]

    api.merges.clear()
    api.comparisons = [{"behind_by": 1}]
    try:
        merge_candidate(api, candidate)
    except ReleaseBlocked as exc:
        assert "main advanced" in str(exc)
    else:
        raise AssertionError("a main change after CI must block merge")
    assert api.merges == []


def _assert_workflow_contract() -> None:
    baseline = (ROOT / ".github" / "workflows" / "baseline-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release-train.yml").read_text(encoding="utf-8")
    assert "pull_request:" in baseline
    assert "name: baseline" in baseline
    assert "pull_request_target:" in release
    assert "queue: max" in release
    assert "ref: main" in release
    assert "release:ready" in release
    assert "dispatch-next" in release
    assert "deploy-and-verify" in release
    assert "WB_CORE_DEPLOY_SSH_KEY" in release
    assert "WB_CORE_DEPLOY_KNOWN_HOSTS" in release


def main() -> int:
    _assert_label_state_machine()
    _assert_scope_is_exact()
    _assert_selection_and_halt()
    _assert_ci_gate_and_branch_update()
    _assert_mutation_and_missing_secret_block()
    _assert_exact_merge_gate()
    _assert_workflow_contract()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
