"""Deterministic coverage for the direct GitHub Release Train."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_train import (  # noqa: E402
    BLOCKED_LABEL,
    DONE_LABEL,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    PRODUCTION_LABEL,
    READY_LABEL,
    REPO_ONLY_LABEL,
    RUNNING_LABEL,
    STANDARD_TASK_LABEL,
    Candidate,
    ReleaseBlocked,
    complete_standard_release,
    enqueue_release,
    halt_merged_release,
    merge_candidate,
    parse_release_enqueue_command,
    prepare_candidate,
    release_enqueue_proof,
    select_candidate,
    set_release_state,
)
from apps.github_release_train_wait import evaluate_release  # noqa: E402


SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeApi:
    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.checks: dict[str, list[dict[str, Any]]] = {}
        self.comparisons: list[dict[str, Any]] = [{"behind_by": 0}]
        self.comments: list[tuple[int, str, str]] = []
        self.comment_times: list[str] = []
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.clock = 0
        self.dispatched: list[tuple[str, str]] = []
        self.merges: list[tuple[int, str]] = []
        self.deleted: list[str] = []

    def ensure_label(self, name: str, color: str, description: str) -> None:
        return None

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]:
        return [
            pull
            for pull in self.pulls.values()
            if label in labels(pull) and (state == "all" or pull.get("state") == state)
        ]

    def list_issue_events(self, number: int) -> list[dict[str, Any]]:
        return list(self.events.get(number, []))

    def _timestamp(self) -> str:
        self.clock += 1
        return (
            datetime(2026, 8, 6, tzinfo=timezone.utc) + timedelta(seconds=self.clock)
        ).isoformat()

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return [
            {
                "id": index,
                "body": body,
                "user": {"login": actor},
                "author_association": "CONTRIBUTOR",
                "created_at": self.comment_times[index - 1],
            }
            for index, (pr, body, actor) in enumerate(self.comments, start=1)
            if pr == number
        ]

    def get_pull(self, number: int) -> dict[str, Any]:
        return self.pulls[number]

    def compare(self, base: str, head: str) -> dict[str, Any]:
        if len(self.comparisons) > 1:
            return self.comparisons.pop(0)
        return self.comparisons[0]

    def update_branch(self, number: int, expected_head_sha: str) -> None:
        self.pulls[number]["head"]["sha"] = SHA_B

    def dispatch_workflow(self, workflow: str, ref: str) -> None:
        self.dispatched.append((workflow, ref))
        if workflow == "baseline-ci.yml":
            head = str(self._running_pull()["head"]["sha"])
            runs = self.checks.setdefault(head, [])
            runs.append(
                {
                    "id": max((int(item.get("id") or 0) for item in runs), default=0) + 1,
                    "name": "baseline",
                    "status": "completed",
                    "conclusion": "success",
                }
            )

    def _running_pull(self) -> dict[str, Any]:
        running = [pull for pull in self.pulls.values() if RUNNING_LABEL in labels(pull)]
        assert len(running) == 1, running
        return running[0]

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        return list(self.checks.get(sha, []))

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]:
        self.merges.append((number, expected_head_sha))
        merge_sha = f"{number:040x}"
        pull = self.pulls[number]
        pull.update(state="closed", merged=True, merge_commit_sha=merge_sha)
        return {"merged": True, "sha": merge_sha}

    def add_labels(self, number: int, values: Iterable[str]) -> None:
        before = labels(self.pulls[number])
        after = before | set(values)
        set_labels(self.pulls[number], after)
        self._record_label_events(number, after - before)

    def set_labels(self, number: int, values: Iterable[str]) -> None:
        before = labels(self.pulls[number])
        after = set(values)
        set_labels(self.pulls[number], after)
        self._record_label_events(number, after - before)

    def _record_label_events(self, number: int, added: Iterable[str]) -> None:
        for label in sorted(added):
            self.events.setdefault(number, []).append(
                {
                    "event": "labeled",
                    "label": {"name": label},
                    "created_at": self._timestamp(),
                }
            )

    def remove_label(self, number: int, label: str) -> None:
        current = labels(self.pulls[number])
        current.discard(label)
        set_labels(self.pulls[number], current)

    def add_comment(self, number: int, body: str) -> None:
        self.comments.append((number, body, "github-actions[bot]"))
        self.comment_times.append(self._timestamp())

    def update_comment(self, comment_id: int, body: str) -> None:
        raise AssertionError("not used")

    def delete_comment(self, comment_id: int) -> None:
        raise AssertionError("not used")

    def close_pull(self, number: int) -> None:
        self.pulls[number]["state"] = "closed"

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)


def labels(pull: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in pull.get("labels") or []
    }


def set_labels(pull: dict[str, Any], values: Iterable[str]) -> None:
    pull["labels"] = [{"name": item} for item in sorted(set(values))]


def make_pull(
    number: int,
    *,
    sha: str = SHA_A,
    scope: str = REPO_ONLY_LABEL,
    state: str = "open",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "draft": False,
        "merged": False,
        "mergeable": True,
        "created_at": f"2026-08-06T00:{number:02d}:00Z",
        "labels": [{"name": STANDARD_TASK_LABEL}, {"name": scope}],
        "pull_request": {"url": f"https://example.invalid/{number}"},
        "base": {"ref": "main"},
        "head": {
            "sha": sha,
            "ref": f"codex/pr-{number}",
            "repo": {"full_name": "orenvlad-ai/wb-core"},
        },
    }


def add_success(api: FakeApi, sha: str) -> None:
    api.checks.setdefault(sha, []).append(
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    )


def enqueue(api: FakeApi, number: int, sha: str, *, actor: str = "orenvlad-ai") -> None:
    command = parse_release_enqueue_command(
        f"/wb-core release enqueue {number} head {sha}"
    )
    assert enqueue_release(
        api,
        command,
        actor=actor,
        association="OWNER",
        actions_owned=True,
    ) == READY_LABEL


def expect_blocked(callable_: Any, contains: str) -> None:
    try:
        callable_()
    except ReleaseBlocked as exc:
        assert contains in str(exc), exc
    else:
        raise AssertionError(f"expected ReleaseBlocked containing {contains!r}")


def test_enqueue_guards() -> None:
    api = FakeApi()
    api.pulls[1] = make_pull(1)
    add_success(api, SHA_A)
    enqueue(api, 1, SHA_A)
    assert READY_LABEL in labels(api.pulls[1])
    assert release_enqueue_proof(api, api.pulls[1]) is not None
    assert api.dispatched == [("release-train.yml", "main")]

    stale = FakeApi()
    stale.pulls[2] = make_pull(2)
    add_success(stale, SHA_A)
    expect_blocked(
        lambda: enqueue_release(
            stale,
            parse_release_enqueue_command(
                f"/wb-core release enqueue 2 head {SHA_B}"
            ),
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
        ),
        "stale",
    )

    failed = FakeApi()
    failed.pulls[3] = make_pull(3)
    failed.checks[SHA_A] = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "failure"}
    ]
    expect_blocked(lambda: enqueue(failed, 3, SHA_A), "successful 'baseline'")

    untrusted = FakeApi()
    untrusted.pulls[4] = make_pull(4)
    add_success(untrusted, SHA_A)
    command = parse_release_enqueue_command(
        f"/wb-core release enqueue 4 head {SHA_A}"
    )
    expect_blocked(
        lambda: enqueue_release(
            untrusted,
            command,
            actor="outside-user",
            association="CONTRIBUTOR",
            actions_owned=True,
        ),
        "OWNER or MEMBER",
    )
    expect_blocked(
        lambda: enqueue_release(
            untrusted,
            command,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=False,
        ),
        "trusted-main Actions",
    )


def test_manual_bypass_concurrency_and_empty_schedule() -> None:
    api = FakeApi()
    manual = make_pull(9)
    set_labels(manual, labels(manual) | {READY_LABEL})
    api.pulls[9] = manual
    assert select_candidate(api) == {"status": "empty", "found": False}

    api.pulls[10] = make_pull(10, sha=SHA_A)
    api.pulls[11] = make_pull(11, sha=SHA_B)
    add_success(api, SHA_A)
    add_success(api, SHA_B)
    enqueue(api, 10, SHA_A)
    enqueue(api, 11, SHA_B)
    selected = select_candidate(api)
    assert selected["pr_number"] == 10

    set_labels(
        api.pulls[10],
        {STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL, READY_LABEL},
    )
    assert release_enqueue_proof(api, api.pulls[10]) is None
    assert select_candidate(api)["pr_number"] == 11

    replay = FakeApi()
    replay.pulls[12] = make_pull(12)
    add_success(replay, SHA_A)
    enqueue(replay, 12, SHA_A)
    set_release_state(replay, 12, BLOCKED_LABEL)
    replay.set_labels(
        12,
        {STANDARD_TASK_LABEL, REPO_ONLY_LABEL, READY_LABEL},
    )
    assert release_enqueue_proof(replay, replay.pulls[12]) is None
    assert select_candidate(replay) == {"status": "empty", "found": False}
    enqueue(replay, 12, SHA_A)
    assert select_candidate(replay)["pr_number"] == 12

    empty = FakeApi()
    assert select_candidate(empty) == {"status": "empty", "found": False}


def test_sync_merge_repo_only_and_halted_live_failure() -> None:
    sync = FakeApi()
    sync.pulls[20] = make_pull(20)
    add_success(sync, SHA_A)
    enqueue(sync, 20, SHA_A)
    sync.comparisons = [{"behind_by": 1}, {"behind_by": 0}]
    expect_blocked(
        lambda: prepare_candidate(
            sync,
            "orenvlad-ai/wb-core",
            20,
            check_name="baseline",
            timeout_seconds=1,
            poll_seconds=0,
        ),
        "enqueue the new exact head",
    )

    repo = FakeApi()
    repo.pulls[21] = make_pull(21)
    add_success(repo, SHA_A)
    enqueue(repo, 21, SHA_A)
    candidate = prepare_candidate(
        repo,
        "orenvlad-ai/wb-core",
        21,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )
    assert isinstance(candidate, Candidate)
    result = merge_candidate(repo, candidate)
    assert complete_standard_release(
        repo, 21, merge_sha=result.merge_sha, contour="repo-only"
    ) == DONE_LABEL
    assert DONE_LABEL in labels(repo.pulls[21])

    live = FakeApi()
    live.pulls[22] = make_pull(22, scope=LIVE_RUNTIME_LABEL)
    add_success(live, SHA_A)
    enqueue(live, 22, SHA_A)
    live_candidate = prepare_candidate(
        live,
        "orenvlad-ai/wb-core",
        22,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )
    live_result = merge_candidate(live, live_candidate)
    assert halt_merged_release(
        live,
        22,
        merge_sha=live_result.merge_sha,
        reason="fake deploy failure",
    ) == HALTED_LABEL
    halted = select_candidate(live)
    assert halted == {"status": "halted", "found": False, "halted_pr_number": 22}


def test_workflow_and_docs_contract() -> None:
    workflow = (ROOT / ".github/workflows/release-train.yml").read_text(encoding="utf-8")
    baseline = (ROOT / ".github/workflows/baseline-ci.yml").read_text(encoding="utf-8")
    implementation = (ROOT / "apps/github_release_train.py").read_text(encoding="utf-8")
    combined = workflow + implementation
    for required in (
        "/wb-core release enqueue ",
        "release:ready",
        "deploy-and-verify",
        "complete-standard",
        "halt-merged",
        "resume-halted",
        "preflight-production-mutation",
        "/wb-core production-mutation complete",
        "finance:migration-deploy-lease",
        'cron: "*/5 * * * *"',
        "group: wb-core-production-release",
    ):
        assert required in combined, required
    for removed in (
        "WB_CORE_" + "ORCHESTRATION_REQUIRED",
        "/wb-core orchestration ",
        "await_loop_agent:",
        "request-agent",
        "await-ui",
    ):
        assert removed not in workflow, removed
    assert "codex_task_orchestrator_smoke.py" not in baseline
    assert "codex_curator_workspace_smoke.py" not in baseline

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    release_doc = (ROOT / "docs/architecture/11_github_release_train.md").read_text(
        encoding="utf-8"
    )
    for source in (agents, release_doc):
        assert "/wb-core release enqueue" in source
        assert "owner acceptance" in source.casefold() or "приёмк" in source.casefold()

    assert "Refuse conflicting release gates" in workflow


def test_waiter_requires_exact_terminal_evidence() -> None:
    merge = "c" * 40
    payload: dict[str, Any] = {
        "number": 30,
        "state": "MERGED",
        "isDraft": False,
        "headRefOid": SHA_A,
        "mergeCommit": {"oid": merge},
        "labels": [
            {"name": DONE_LABEL},
            {"name": STANDARD_TASK_LABEL},
            {"name": REPO_ONLY_LABEL},
        ],
        "comments": [],
    }
    assert evaluate_release(payload)["disposition"] == "OWN_ACTION"
    payload["comments"] = [
        {
            "author": {"login": "github-actions[bot]"},
            "body": (
                "<!-- wb-core-release-completion-proof "
                f"contour=repo-only merge={merge} pr=30 -->"
            ),
        }
    ]
    assert evaluate_release(payload)["disposition"] == "TERMINAL_SUCCESS"

    payload.update(state="OPEN", mergeCommit=None, comments=[])
    payload["labels"] = [
        {"name": BLOCKED_LABEL},
        {"name": STANDARD_TASK_LABEL},
        {"name": REPO_ONLY_LABEL},
    ]
    blocked = evaluate_release(payload)
    assert blocked["disposition"] == "OWN_ACTION" and blocked["exit_code"] == 5

    payload["labels"] = [
        {"name": READY_LABEL},
        {"name": RUNNING_LABEL},
        {"name": STANDARD_TASK_LABEL},
        {"name": REPO_ONLY_LABEL},
    ]
    assert evaluate_release(payload)["release_state"] == RUNNING_LABEL

    payload["labels"] = [
        {"name": "release:superseded"},
        {"name": STANDARD_TASK_LABEL},
        {"name": REPO_ONLY_LABEL},
    ]
    assert evaluate_release(payload)["disposition"] == "TERMINAL_INELIGIBLE"


def main() -> int:
    test_enqueue_guards()
    test_manual_bypass_concurrency_and_empty_schedule()
    test_sync_merge_repo_only_and_halted_live_failure()
    test_workflow_and_docs_contract()
    test_waiter_requires_exact_terminal_evidence()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
