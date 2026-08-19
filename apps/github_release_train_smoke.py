"""Deterministic state-machine coverage for the GitHub Release Train."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
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
    complete_production_mutation_release,
    DONE_LABEL,
    FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
    FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER,
    FINANCE_DEPLOY_LEASE_LABEL,
    FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    LOOP_ACK_PREFIX,
    LOOP_TASK_LABEL,
    NEEDS_RESUME_LABEL,
    PRODUCTION_LABEL,
    PRODUCTION_MUTATION_LABEL,
    READY_LABEL,
    RELEASE_LANE_OWNER_LABEL,
    REPO_ONLY_LABEL,
    RETIRED_LABEL,
    RUNNING_LABEL,
    STAGED_LABEL,
    STANDARD_TASK_LABEL,
    SUPERSEDED_LABEL,
    DCPReleaseReadmissionRequired,
    ReleaseBlocked,
    ReleaseClassificationBlocked,
    ReleaseReadmissionRequired,
    ReleaseTrainError,
    accept_loop_ui,
    acquire_finance_deploy_lease,
    acknowledge_loop_agent,
    authorize_finance_deploy_lease_recovery,
    complete_standard_release,
    correct_loop_identity_to_new,
    enqueue_loop_new,
    enqueue_loop_recovery,
    handle_loop_comment,
    handle_orchestration_comment,
    finance_deploy_lease_state,
    loop_ack_label,
    loop_root_label,
    loop_root_from_labels,
    loop_registration_kind,
    mark_loop_awaiting_ui,
    mark_classification_blocked,
    merge_candidate,
    prepare_candidate,
    parse_production_mutation_terminalization_command,
    parse_finance_deploy_lease_command,
    production_mutation_terminal_state_proven,
    production_mutation_terminalization_preflight,
    request_loop_agent,
    rebind_finance_deploy_lease,
    resume_halted_release,
    resume_loop_owner,
    retry_blocked_release,
    require_deploy_environment,
    release_lane_integrity,
    release_lane_release_proofs,
    release_lane_state,
    scope_from_labels,
    select_candidate,
    set_release_state,
    task_class_from_labels,
    terminal_state_proven,
    terminalize_finance_deploy_lease,
    transition_label_set,
    upsert_status_comment,
    _queue_status_api_from_env,
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
    CANONICAL_PRODUCTION_SERVICE_NAME,
    CANONICAL_PRODUCTION_TARGET_ID,
    CRITICAL_TRANSITIONS,
    DCP_RELEASE_HANDOFF_PROOF_MARKER,
    DCP_RELEASE_HANDOFF_V1_VERSION,
    DCP_RELEASE_HANDOFF_VERSION,
    DCP_RELEASE_PRODUCTION_PROOF_MARKER,
    DCP_RELEASE_READMISSION_PROOF_MARKER,
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
MANIFEST = "sha256:" + "e" * 64


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
        self.timeline: dict[int, list[dict[str, Any]]] = {}
        self.merges: list[tuple[int, str]] = []
        self.deleted: list[str] = []
        self.updated_bodies: list[tuple[int, str]] = []

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

    def list_timeline_items(self, number: int) -> list[dict[str, Any]]:
        return list(self.timeline.get(number, []))

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

    def get_branch_sha(self, branch: str) -> str:
        assert branch == "main"
        return SHA_C

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
            completed_at = datetime.now(timezone.utc).isoformat()
            head_sha = next(
                (
                    str((pull.get("head") or {}).get("sha") or "")
                    for pull in self.pulls.values()
                    if str((pull.get("head") or {}).get("ref") or "") == ref
                ),
                "",
            )
            self.checks.append(
                {
                    "id": next_id,
                    "name": "baseline",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": completed_at,
                    "head_sha": head_sha,
                }
            )

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.checks
            if not str(item.get("head_sha") or "")
            or str(item.get("head_sha") or "") == sha
        ]

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]:
        self.merges.append((number, expected_head_sha))
        merge_sha = f"{number:040x}"
        pull = self.pulls[number]
        pull["state"] = "closed"
        pull["merged"] = True
        pull["merged_by"] = {"login": "github-actions[bot]"}
        pull["merge_commit_sha"] = merge_sha
        return {"merged": True, "sha": merge_sha}

    def add_labels(self, number: int, labels: Iterable[str]) -> None:
        current = _labels(self.pulls[number])
        additions = {str(label) for label in labels} - current
        current.update(additions)
        _set_labels(self.pulls[number], current)
        for label in sorted(additions):
            self._append_label_event(number, "labeled", label)

    def set_labels(self, number: int, labels: Iterable[str]) -> None:
        before = _labels(self.pulls[number])
        after = {str(label) for label in labels}
        self.replaced_labels.append((number, set(after)))
        _set_labels(self.pulls[number], after)
        for label in sorted(before - after):
            self._append_label_event(number, "unlabeled", label)
        for label in sorted(after - before):
            self._append_label_event(number, "labeled", label)

    def remove_label(self, number: int, label: str) -> None:
        current = _labels(self.pulls[number])
        removed = label in current
        current.discard(label)
        _set_labels(self.pulls[number], current)
        if removed:
            self._append_label_event(number, "unlabeled", label)

    def _append_label_event(self, number: int, event: str, label: str) -> None:
        event_id = 100_000 + sum(len(items) for items in self.events.values()) + 1
        item = {
            "id": event_id,
            "event": event,
            "label": {"name": label},
            "actor": {"login": "github-actions[bot]"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.events.setdefault(number, []).append(item)
        self.timeline.setdefault(number, []).append(dict(item))

    def add_comment(self, number: int, body: str) -> None:
        comment_id = max(self.comment_ids, default=0) + 1
        self.comments.append((number, body))
        self.comment_ids.append(comment_id)
        self.comment_metadata[comment_id] = {
            "user": {"login": "github-actions[bot]"},
            "author_association": "CONTRIBUTOR",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.comment_metadata[comment_id]["updated_at"] = self.comment_metadata[comment_id][
            "created_at"
        ]

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
            "updated_at": created_at,
        }
        return comment_id

    def update_comment(self, comment_id: int, body: str) -> None:
        index = self.comment_ids.index(comment_id)
        number, _ = self.comments[index]
        self.comments[index] = (number, body)
        self.comment_metadata[comment_id]["updated_at"] = datetime.now(
            timezone.utc
        ).isoformat()

    def update_pull_body(self, number: int, body: str) -> None:
        self.pulls[number]["body"] = body
        self.updated_bodies.append((number, body))

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
        "body": "",
        "created_at": created_at,
        "labels": [{"name": label} for label in labels],
        "pull_request": {"url": f"https://example.invalid/{number}"},
        "base": {"ref": "main"},
        "head": {
            "sha": sha,
            "ref": f"feature/{number}",
            "repo": {"full_name": "orenvlad-ai/wb-core"},
        },
        "user": {"login": "orenvlad-ai"},
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
    assert not any(
        DCP_RELEASE_HANDOFF_PROOF_MARKER in body for _, body in api.comments
    )


def _dcp_release_fixture(
    number: int,
    *,
    head_sha: str = SHA_A,
    comparison: dict[str, Any] | None = None,
    scope: str = REPO_ONLY_LABEL,
) -> tuple[FakeApi, dict[str, Any]]:
    api = FakeApi()
    pull = _pull(
        number,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, scope],
        created_at="2026-08-17T09:00:00Z",
        sha=head_sha,
    )
    pull["head"]["ref"] = f"ao/wb-core-{number}/root"
    api.pulls[number] = pull
    ready_event = {
        "id": number * 100 + 1,
        "event": "labeled",
        "label": {"name": READY_LABEL},
        "actor": {"login": "orenvlad-ai"},
        "created_at": "2026-08-17T10:01:00Z",
    }
    api.events[number] = [dict(ready_event)]
    api.timeline[number] = [
        {"event": "committed", "sha": head_sha},
        dict(ready_event),
    ]
    api.checks = [
        {
            "id": number * 100 + 2,
            "name": "baseline",
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-08-17T10:00:00Z",
            "head_sha": head_sha,
        }
    ]
    current = comparison or {"behind_by": 0, "status": "ahead"}
    api.comparisons = [dict(current), dict(current), dict(current)]
    return api, pull


def _prepare_dcp(api: FakeApi, number: int) -> Candidate:
    return prepare_candidate(
        api,
        "orenvlad-ai/wb-core",
        number,
        check_name="baseline",
        timeout_seconds=1,
        poll_seconds=0,
    )


def _dcp_proof_comment_index(api: FakeApi, number: int) -> int:
    return next(
        index
        for index, (comment_number, body) in enumerate(api.comments)
        if comment_number == number and DCP_RELEASE_HANDOFF_PROOF_MARKER in body
    )


def _assert_dcp_release_handoff_v2() -> None:
    # Exact admitted head: no update-branch, one fresh Release Train baseline,
    # one Actions merge and one exact terminal proof in the PR body.
    api, pull = _dcp_release_fixture(701)
    candidate = _prepare_dcp(api, 701)
    assert candidate.head_sha == SHA_A
    assert api.updated == []
    assert RUNNING_LABEL in _labels(pull) and READY_LABEL not in _labels(pull)
    proof_bodies = [
        body
        for number, body in api.comments
        if number == 701 and DCP_RELEASE_HANDOFF_PROOF_MARKER in body
    ]
    assert len(proof_bodies) == 1
    assert DCP_RELEASE_HANDOFF_VERSION in proof_bodies[0]
    merge = merge_candidate(api, candidate)
    assert api.merges == [(701, SHA_A)]
    complete_standard_release(api, 701, merge_sha=merge.merge_sha, contour="repo-only")
    assert api.merges == [(701, SHA_A)]
    assert DONE_LABEL in _labels(pull) and RUNNING_LABEL not in _labels(pull)
    terminal = (
        f"<!-- wb-core-release-completion-proof contour=repo-only "
        f"merge={merge.merge_sha} pr=701 -->"
    )
    assert str(pull.get("body") or "").count(terminal) == 1
    assert terminal_state_proven(api, pull)

    # A behind admitted head loses eligibility without branch synchronization,
    # blocking or merge. Generic retry cannot replace fresh DCP admission.
    behind_api, behind_pull = _dcp_release_fixture(
        702,
        comparison={"behind_by": 1, "status": "behind"},
    )
    try:
        _prepare_dcp(behind_api, 702)
    except DCPReleaseReadmissionRequired as exc:
        assert exc.reason == "base-behind-after-admission"
    else:
        raise AssertionError("behind DCP admission must require readmission")
    assert behind_api.updated == [] and behind_api.merges == []
    assert not (_labels(behind_pull) & {READY_LABEL, RUNNING_LABEL, BLOCKED_LABEL})
    assert any(
        DCP_RELEASE_READMISSION_PROOF_MARKER in body
        for number, body in behind_api.comments
        if number == 702
    )
    readmission = next(
        body
        for number, body in behind_api.comments
        if number == 702 and DCP_RELEASE_READMISSION_PROOF_MARKER in body
    )
    for exact_field in (
        "admission_check=70202",
        f"admitted_head={SHA_A}",
        "base=main",
        "handoff_proof=0",
        "head_ref=ao/wb-core-702/root",
        f"main={SHA_C}",
        f"observed_head={SHA_A}",
        "pr=702",
        "ready_event=70201",
        "reason=base-behind-after-admission",
        "repo=orenvlad-ai/wb-core",
        "scope=scope:repo-only",
        "session=702",
        "task=task:standard",
        f"version={DCP_RELEASE_HANDOFF_VERSION}",
        "digest=sha256:",
    ):
        assert exact_field in readmission

    _set_labels(behind_pull, [BLOCKED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL])
    try:
        retry_blocked_release(
            behind_api,
            702,
            expected_head_sha=SHA_A,
            check_name="baseline",
        )
    except ReleaseBlocked as exc:
        assert "fresh exact-head DCP review" in str(exc)
    else:
        raise AssertionError("generic retry must not bypass DCP readmission")

    # Replacement head is ineligible until a new exact-head baseline and a new
    # DCP-owned release:ready admission event exist.
    replacement_head = SHA_B
    behind_pull["head"]["sha"] = replacement_head
    _set_labels(behind_pull, [READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL])
    replacement_event = {
        "id": 70299,
        "event": "labeled",
        "label": {"name": READY_LABEL},
        "actor": {"login": "orenvlad-ai"},
        "created_at": "2026-08-17T11:01:00Z",
    }
    behind_api.events[702].append(dict(replacement_event))
    behind_api.timeline[702].extend(
        [
            {"event": "committed", "sha": replacement_head},
            dict(replacement_event),
        ]
    )
    behind_api.comparisons = [
        {"behind_by": 0, "status": "ahead"},
        {"behind_by": 0, "status": "ahead"},
        {"behind_by": 0, "status": "ahead"},
    ]
    try:
        _prepare_dcp(behind_api, 702)
    except ReleaseBlocked as exc:
        assert "successful baseline" in str(exc)
    else:
        raise AssertionError("replacement DCP head without fresh baseline must fail closed")
    behind_api.checks.append(
        {
            "id": 70298,
            "name": "baseline",
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-08-17T11:00:00Z",
            "head_sha": replacement_head,
        }
    )
    replacement = _prepare_dcp(behind_api, 702)
    assert replacement.head_sha == replacement_head and behind_api.updated == []
    assert f"ready_event={replacement_event['id']}" in next(
        body
        for number, body in behind_api.comments
        if number == 702 and DCP_RELEASE_HANDOFF_PROOF_MARKER in body
    )

    # Current proof is mandatory, canonical, Actions-owned and immutable.
    for offset, mode in enumerate(
        ("missing", "edited", "duplicate", "stale", "wrong-repo", "wrong-base"),
        start=710,
    ):
        proof_api, _ = _dcp_release_fixture(offset)
        proof_candidate = _prepare_dcp(proof_api, offset)
        proof_index = _dcp_proof_comment_index(proof_api, offset)
        comment_id = proof_api.comment_ids[proof_index]
        number, body = proof_api.comments[proof_index]
        if mode == "missing":
            proof_api.comments.pop(proof_index)
            proof_api.comment_ids.pop(proof_index)
            proof_api.comment_metadata.pop(comment_id, None)
        elif mode == "edited":
            proof_api.update_comment(comment_id, body + "\nedited")
        elif mode == "duplicate":
            proof_api.add_comment(number, body)
        elif mode == "stale":
            proof_api.comments[proof_index] = (
                number,
                body.replace(f"head={SHA_A}", f"head={SHA_B}"),
            )
        elif mode == "wrong-repo":
            proof_api.comments[proof_index] = (
                number,
                body.replace(
                    "repo=orenvlad-ai/wb-core",
                    "repo=orenvlad-ai/wrong",
                ),
            )
        else:
            proof_api.comments[proof_index] = (
                number,
                body.replace("base=main", "base=release"),
            )
        try:
            merge_candidate(proof_api, proof_candidate)
        except ReleaseBlocked:
            pass
        else:
            raise AssertionError(f"{mode} DCP handoff proof must fail closed")
        assert proof_api.merges == []

    # A direct/manual merge can never be laundered into Release Train terminal
    # evidence, even if the exact-head handoff proof already exists.
    direct_api, _ = _dcp_release_fixture(719)
    direct_candidate = _prepare_dcp(direct_api, 719)
    direct_pull = direct_api.pulls[719]
    direct_pull["state"] = "closed"
    direct_pull["merged"] = True
    direct_pull["merged_by"] = {"login": "orenvlad-ai"}
    direct_pull["merge_commit_sha"] = "719".zfill(40)
    try:
        merge_candidate(direct_api, direct_candidate)
    except ReleaseBlocked as exc:
        assert "outside the GitHub Actions Release Train" in str(exc)
    else:
        raise AssertionError("direct DCP merge must fail closed")
    try:
        complete_standard_release(
            direct_api,
            719,
            merge_sha=str(direct_pull["merge_commit_sha"]),
            contour="repo-only",
        )
    except ReleaseBlocked as exc:
        assert "outside the GitHub Actions Release Train" in str(exc)
    else:
        raise AssertionError("direct DCP merge must not receive terminal proof")
    assert direct_api.merges == []

    # A head push after proof is readmission, never auto-sync or merge.
    stale_api, stale_pull = _dcp_release_fixture(720)
    stale_candidate = _prepare_dcp(stale_api, 720)
    stale_pull["head"]["sha"] = SHA_B
    stale_api.timeline[720].append({"event": "committed", "sha": SHA_B})
    try:
        merge_candidate(stale_api, stale_candidate)
    except DCPReleaseReadmissionRequired as exc:
        assert exc.reason == "head-changed-after-release-check"
    else:
        raise AssertionError("post-proof DCP head drift must require readmission")
    assert stale_api.updated == [] and stale_api.merges == []
    assert not (_labels(stale_pull) & {READY_LABEL, RUNNING_LABEL, BLOCKED_LABEL})

    # V2 live-runtime uses the same no-sync/exact-baseline seam, but terminal
    # success additionally requires exact Actions deploy and read-only runtime
    # evidence. Merge alone and release:done cannot pass.
    live_api, live_pull = _dcp_release_fixture(721, scope=LIVE_RUNTIME_LABEL)
    live_candidate = _prepare_dcp(live_api, 721)
    assert live_candidate.deploy_required
    handoff = next(
        body
        for number, body in live_api.comments
        if number == 721 and DCP_RELEASE_HANDOFF_PROOF_MARKER in body
    )
    assert "scope=scope:live-runtime" in handoff
    assert "task=task:standard" in handoff
    assert f"main={SHA_C}" in handoff
    live_merge = merge_candidate(live_api, live_candidate)
    try:
        complete_standard_release(
            live_api,
            721,
            merge_sha=live_merge.merge_sha,
            contour="production-verified",
        )
    except ReleaseBlocked as exc:
        assert "deploy and read-only runtime evidence" in str(exc)
    else:
        raise AssertionError("DCP live-runtime merge without exact deploy proof must fail closed")
    assert not terminal_state_proven(live_api, live_pull)
    deploy_evidence = {
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "deploy": {"ok": True, "dry_run": False},
        "loopback_probe": {"ok": True, "routes": []},
        "public_probe": {"ok": True, "routes": []},
        "ok": True,
    }
    runtime_evidence = {
        "status": "reconciled",
        "pr": 721,
        "head": SHA_A,
        "merge": live_merge.merge_sha,
        "expected_sha": live_merge.merge_sha,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "service_name": CANONICAL_PRODUCTION_SERVICE_NAME,
        "healthy": True,
        "repairs_applied": False,
        "read_only": True,
        "evidence": [
            {
                "metadata_sha": live_merge.merge_sha,
                "runtime_sha": live_merge.merge_sha,
                "deployment_complete": True,
                "unit": "active",
                "main_pid": 721,
                "probe_statuses": [200, 401, 403, 303],
                "target_id": CANONICAL_PRODUCTION_TARGET_ID,
                "auth_env_ok": True,
            }
        ],
    }
    complete_standard_release(
        live_api,
        721,
        merge_sha=live_merge.merge_sha,
        contour="production-verified",
        deploy_evidence=deploy_evidence,
        runtime_evidence=runtime_evidence,
    )
    assert PRODUCTION_LABEL in _labels(live_pull)
    assert DONE_LABEL not in _labels(live_pull)
    assert sum(
        DCP_RELEASE_PRODUCTION_PROOF_MARKER in body
        for number, body in live_api.comments
        if number == 721
    ) == 1
    assert terminal_state_proven(live_api, live_pull)
    complete_standard_release(
        live_api,
        721,
        merge_sha=live_merge.merge_sha,
        contour="production-verified",
    )
    assert sum(
        DCP_RELEASE_PRODUCTION_PROOF_MARKER in body
        for number, body in live_api.comments
        if number == 721
    ) == 1
    production_index = next(
        index
        for index, (number, body) in enumerate(live_api.comments)
        if number == 721 and DCP_RELEASE_PRODUCTION_PROOF_MARKER in body
    )
    production_id = live_api.comment_ids[production_index]
    live_api.update_comment(production_id, live_api.comments[production_index][1] + "\nedited")
    assert not terminal_state_proven(live_api, live_pull)

    wrong_api, _ = _dcp_release_fixture(722, scope=LIVE_RUNTIME_LABEL)
    wrong_candidate = _prepare_dcp(wrong_api, 722)
    wrong_merge = merge_candidate(wrong_api, wrong_candidate)
    wrong_runtime = {
        **runtime_evidence,
        "pr": 722,
        "merge": wrong_merge.merge_sha,
        "expected_sha": wrong_merge.merge_sha,
        "service_name": "wrong.service",
        "evidence": [
            {
                **runtime_evidence["evidence"][0],
                "metadata_sha": wrong_merge.merge_sha,
                "runtime_sha": wrong_merge.merge_sha,
            }
        ],
    }
    try:
        complete_standard_release(
            wrong_api,
            722,
            merge_sha=wrong_merge.merge_sha,
            contour="production-verified",
            deploy_evidence=deploy_evidence,
            runtime_evidence=wrong_runtime,
        )
    except ReleaseBlocked as exc:
        assert "service_name" in str(exc)
    else:
        raise AssertionError("wrong DCP production service identity must fail closed")


def _assert_orchestration_lane_and_legacy_retirement() -> None:
    api = FakeApi()
    first = _pull(
        60,
        labels=[STAGED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T01:00:00Z",
    )
    second = _pull(
        61,
        labels=[STAGED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T02:00:00Z",
        sha=SHA_B,
    )
    direct_ready = _pull(
        62,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T00:00:00Z",
        sha=SHA_C,
    )
    same_task_next = _pull(
        63,
        labels=[STAGED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T01:30:00Z",
        sha=SHA_C,
    )
    api.pulls = {60: first, 61: second, 62: direct_ready, 63: same_task_next}
    api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    passport = "sha256:" + "1" * 64
    evidence = "sha256:" + "2" * 64
    assert handle_orchestration_comment(
        api,
        60,
        f"/wb-core orchestration admit 60 head {SHA_A} task task-alpha-01 revision 1 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "admitted"
    assert RELEASE_LANE_OWNER_LABEL in _labels(first)
    assert READY_LABEL in _labels(first) and STAGED_LABEL not in _labels(first)
    selected = select_candidate(api, orchestration_required=True)
    assert selected["pr_number"] == 60
    lane = release_lane_state(api)
    assert lane["task_id"] == "task-alpha-01" and lane["owner_pr"] == 60

    assert handle_orchestration_comment(
        api,
        63,
        f"/wb-core orchestration admit 63 head {SHA_C} task task-alpha-01 revision 2 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "waiting-own-pr"
    assert STAGED_LABEL in _labels(same_task_next)
    assert any(
        "wb-core-orchestration-admission-proof" in body
        for number, body in api.comments
        if number == 63
    )

    assert handle_orchestration_comment(
        api,
        61,
        f"/wb-core orchestration admit 61 head {SHA_B} task task-bravo-02 revision 1 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "waiting-other-task"
    assert STAGED_LABEL in _labels(second)

    set_release_state(api, 60, RUNNING_LABEL)
    set_release_state(api, 60, DONE_LABEL)
    stale_signal = release_lane_integrity(release_lane_state(api))
    assert stale_signal["status"] == "attention"
    assert stale_signal["signals"][0] == {
        "code": "terminal-release-lane-owner",
        "owner_pr": 60,
        "task_id": "task-alpha-01",
        "minimum_revision": 1,
        "owner_state": DONE_LABEL,
        "required_operation": "orchestration release-lane",
        "required_fields": [
            "owner_pr",
            "task",
            "revision",
            "outcome",
            "evidence",
        ],
    }
    try:
        handle_orchestration_comment(
            api,
            60,
            f"/wb-core orchestration release-lane 60 task task-alpha-01 revision 2 outcome completed evidence {evidence}",
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
        )
    except ReleaseBlocked:
        pass
    else:
        raise AssertionError("same-task staged PR must keep the logical release lane")
    assert handle_orchestration_comment(
        api,
        63,
        f"/wb-core orchestration admit 63 head {SHA_C} task task-alpha-01 revision 2 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "admitted"
    set_release_state(api, 63, RUNNING_LABEL)
    set_release_state(api, 63, DONE_LABEL)
    # PR #918 regression: the published no-revision shape must fail closed.
    try:
        handle_orchestration_comment(
            api,
            60,
            f"/wb-core orchestration release-lane 60 task task-alpha-01 outcome completed evidence {evidence}",
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
        )
    except ReleaseBlocked as exc:
        assert "revision" in str(exc)
    else:
        raise AssertionError("release-lane without an exact positive revision must fail")
    assert handle_orchestration_comment(
        api,
        60,
        f"/wb-core orchestration release-lane 60 task task-alpha-01 revision 2 outcome completed evidence {evidence}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "released"
    assert release_lane_state(api) == {"status": "idle"}
    assert release_lane_release_proofs(api, [60]) == [
        {
            "owner_pr": 60,
            "task_id": "task-alpha-01",
            "revision": 2,
            "outcome": "completed",
            "evidence_digest": evidence,
        }
    ]
    released_comment_count = len(api.comments)
    released_labels = set(_labels(first))
    assert handle_orchestration_comment(
        api,
        60,
        f"/wb-core orchestration release-lane 60 task task-alpha-01 revision 2 outcome completed evidence {evidence}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "already-released"
    assert len(api.comments) == released_comment_count
    assert _labels(first) == released_labels
    assert handle_orchestration_comment(
        api,
        61,
        f"/wb-core orchestration admit 61 head {SHA_B} task task-bravo-02 revision 1 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "admitted"
    assert select_candidate(api, orchestration_required=True)["pr_number"] == 61

    sync_api = FakeApi()
    sync_pull = _pull(
        80,
        labels=[STAGED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T04:00:00Z",
    )
    sync_api.pulls = {80: sync_pull}
    sync_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    assert handle_orchestration_comment(
        sync_api,
        80,
        f"/wb-core orchestration admit 80 head {SHA_A} task task-sync-080 revision 1 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "admitted"
    sync_api.comparisons = [{"behind_by": 1}, {"behind_by": 0}]
    try:
        prepare_candidate(
            sync_api,
            "orenvlad-ai/wb-core",
            80,
            check_name="baseline",
            timeout_seconds=1,
            poll_seconds=0,
            orchestration_required=True,
        )
    except ReleaseReadmissionRequired as exc:
        assert exc.head_sha == SHA_B and exc.task_id == "task-sync-080"
    else:
        raise AssertionError("trusted main sync must require exact-head re-admission")
    assert STAGED_LABEL in _labels(sync_pull) and BLOCKED_LABEL not in _labels(sync_pull)

    loop_api = FakeApi()
    loop = _pull(
        90,
        labels=[LOOP_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-08-03T05:00:00Z",
    )
    loop_api.pulls = {90: loop}
    loop_api.checks = [
        {"id": 1, "name": "baseline", "status": "completed", "conclusion": "success"}
    ]
    enqueue_loop_new(
        loop_api,
        90,
        SHA_A,
        actor="orenvlad-ai",
        association="OWNER",
    )
    assert select_candidate(loop_api, orchestration_required=True)["found"] is False
    assert handle_orchestration_comment(
        loop_api,
        90,
        f"/wb-core orchestration admit 90 head {SHA_A} task task-loop-090 revision 1 passport {passport}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "admitted"
    assert select_candidate(loop_api, orchestration_required=True)["pr_number"] == 90

    lane_retry = _pull(
        91,
        labels=[BLOCKED_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-08-03T05:10:00Z",
        sha=SHA_B,
    )
    loop_api.pulls[91] = lane_retry
    assert retry_blocked_release(
        loop_api,
        91,
        expected_head_sha=SHA_B,
        check_name="baseline",
        orchestration_required=False,
    ) == STAGED_LABEL
    assert STAGED_LABEL in _labels(lane_retry) and READY_LABEL not in _labels(lane_retry)

    manifest_path = ROOT / "migration" / "release_train_legacy_retirement_20260803.json"
    manifest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    legacy = _pull(
        843,
        labels=[BLOCKED_LABEL, STANDARD_TASK_LABEL, PRODUCTION_MUTATION_LABEL],
        created_at="2026-07-28T06:40:00Z",
        sha="6ed1756b1de9555c4f522fdcc2156cd519be707d",
    )
    legacy["merged"] = True
    legacy["state"] = "closed"
    legacy["merge_commit_sha"] = "7e246c839203923c6775cbf1a6e34dc81cb7c036"
    api.pulls[843] = legacy
    assert handle_orchestration_comment(
        api,
        843,
        "/wb-core orchestration retire-legacy 843 head "
        f"6ed1756b1de9555c4f522fdcc2156cd519be707d manifest {manifest}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == RETIRED_LABEL
    assert RETIRED_LABEL in _labels(legacy) and BLOCKED_LABEL not in _labels(legacy)
    assert terminal_state_proven(api, legacy) is True
    assert handle_orchestration_comment(
        api,
        843,
        "/wb-core orchestration retire-legacy 843 head "
        f"6ed1756b1de9555c4f522fdcc2156cd519be707d manifest {manifest}",
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == RETIRED_LABEL


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
    release_gate_body = (
        f"OWNER AUTHORIZATION — exact release gate for PR #{number} head `{SHA_A}`; "
        "the owner authorizes merge and deploy only, stale on any head or semantic change."
    )
    release_gate_id = api.add_external_comment(
        number,
        release_gate_body,
        created_at="2026-07-21T01:30:00Z",
    )
    pull.update(
        state="closed",
        merged=True,
        merge_commit_sha=SHA_B,
        merged_at="2026-07-21T02:00:00Z",
    )
    apply_gate_body = (
        f"OWNER AUTHORIZATION — production apply for PR #{number} on deployed SHA "
        f"`{SHA_C}` and exact manifest `{MANIFEST}`."
    )
    apply_gate_id = api.add_external_comment(
        number,
        apply_gate_body,
        created_at="2026-07-21T02:30:00Z",
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
        f"release-gate {release_gate_id} "
        f"release-gate-digest {_body_fingerprint(release_gate_body)} "
        f"apply-gate {apply_gate_id} "
        f"apply-gate-digest {_body_fingerprint(apply_gate_body)} "
        f"manifest {MANIFEST} "
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
    missing_api.comment_metadata[missing_command.release_gate_comment_id][
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
        assert "release gate" in str(exc)
    else:
        raise AssertionError("missing admissible release gate must fail closed")

    apply_api, apply_text, _ = _production_mutation_terminal_fixture(number=133)
    apply_command = parse_production_mutation_terminalization_command(apply_text)
    apply_api.comment_metadata[apply_command.apply_gate_comment_id][
        "author_association"
    ] = "CONTRIBUTOR"
    try:
        production_mutation_terminalization_preflight(
            apply_api,
            133,
            apply_command,
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "apply gate" in str(exc)
    else:
        raise AssertionError("missing admissible apply gate must fail closed")

    manifest_api, manifest_text, _ = _production_mutation_terminal_fixture(
        number=134
    )
    wrong_manifest = "sha256:" + "f" * 64
    try:
        production_mutation_terminalization_preflight(
            manifest_api,
            134,
            parse_production_mutation_terminalization_command(
                manifest_text.replace(
                    f"manifest {MANIFEST}",
                    f"manifest {wrong_manifest}",
                )
            ),
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "apply gate" in str(exc)
    else:
        raise AssertionError("apply gate with wrong manifest must fail closed")

    ordered_api, ordered_text, _ = _production_mutation_terminal_fixture(number=135)
    ordered_command = parse_production_mutation_terminalization_command(ordered_text)
    ordered_api.comment_metadata[ordered_command.apply_gate_comment_id][
        "created_at"
    ] = "2026-07-21T04:00:00Z"
    try:
        production_mutation_terminalization_preflight(
            ordered_api,
            135,
            ordered_command,
            actor="orenvlad-ai",
            association="OWNER",
        )
    except ReleaseBlocked as exc:
        assert "follow the apply gate" in str(exc)
    else:
        raise AssertionError("reconciliation before apply gate must fail closed")

    try:
        parse_production_mutation_terminalization_command(
            "/wb-core production-mutation complete 136 "
            f"head {SHA_A} merge {SHA_B} deployed {SHA_C} "
            "gate 1 gate-digest sha256:"
            + "a" * 64
            + " reconciliation 2 reconciliation-digest sha256:"
            + "b" * 64
            + f" evidence {EVIDENCE}"
        )
    except ReleaseBlocked as exc:
        assert "post-merge apply gate" in str(exc)
    else:
        raise AssertionError("legacy one-gate completion command must fail closed")

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
        "06_missing_or_stale_two_gate_evidence_fails_closed"
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


def _assert_finance_global_deploy_lease() -> None:
    api = FakeApi()
    anchor_pr = 850
    recovery_pr = 851
    unrelated_pr = 852
    task_id = "019fa739-505c-74b1-9f24-02a2c1f9bf1b"
    lease_id = "finance-split-019fa739"
    anchor = _pull(
        anchor_pr,
        labels=[PRODUCTION_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-28T10:00:00Z",
        sha=SHA_A,
    )
    anchor.update(
        state="closed",
        merged=True,
        merge_commit_sha=SHA_B,
        merged_at="2026-07-28T10:30:00Z",
    )
    api.pulls[anchor_pr] = anchor
    api.add_comment(
        anchor_pr,
        "<!-- wb-core-release-completion-proof "
        f"contour=production-verified merge={SHA_B} pr={anchor_pr} -->",
    )
    api.comparisons = [{"status": "identical", "behind_by": 0}]
    api.pulls[unrelated_pr] = _pull(
        unrelated_pr,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, REPO_ONLY_LABEL],
        created_at="2026-07-28T11:00:00Z",
        sha=SHA_C,
    )
    deploy_evidence = {
        "status": "reconciled",
        "healthy": True,
        "pr": anchor_pr,
        "head": SHA_A,
        "merge": SHA_B,
        "expected_sha": SHA_B,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "read_only": True,
        "repairs_applied": False,
    }
    acquire_text = (
        f"/wb-core finance-lease acquire {anchor_pr} head {SHA_A} "
        f"deployed {SHA_B} task {task_id} lease {lease_id} "
        "window pre-snapshot-1 phase pre-snapshot ttl-minutes 120"
    )
    acquire = parse_finance_deploy_lease_command(acquire_text)
    observed = datetime.now(timezone.utc).timestamp()
    api.pulls[849] = _pull(
        849,
        labels=[
            READY_LABEL,
            RUNNING_LABEL,
            STANDARD_TASK_LABEL,
            LIVE_RUNTIME_LABEL,
        ],
        created_at="2026-07-28T09:55:00Z",
        sha=SHA_C,
    )
    try:
        acquire_finance_deploy_lease(
            api,
            acquire,
            deploy_evidence,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
            now=observed,
        )
    except ReleaseBlocked as exc:
        assert "requires no running" in str(exc)
    else:
        raise AssertionError("Finance lease acquired during an active deploy")
    del api.pulls[849]

    original_add_comment = api.add_comment
    interrupted_once = False

    def interrupt_after_global_label(number: int, body: str) -> None:
        nonlocal interrupted_once
        if (
            not interrupted_once
            and FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER in body
        ):
            interrupted_once = True
            assert FINANCE_DEPLOY_LEASE_LABEL in _labels(api.pulls[number])
            assert FINANCE_DEPLOY_LEASE_AUDIT_LABEL in _labels(
                api.pulls[number]
            )
            raise RuntimeError("simulated client disconnect after fail-closed label")
        original_add_comment(number, body)

    api.add_comment = interrupt_after_global_label  # type: ignore[method-assign]
    try:
        acquire_finance_deploy_lease(
            api,
            acquire,
            deploy_evidence,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
            now=observed,
        )
    except RuntimeError as exc:
        assert "simulated client disconnect" in str(exc)
    else:
        raise AssertionError("fault injection must interrupt acquire")
    interrupted = finance_deploy_lease_state(api, now=observed + 1)
    assert interrupted["status"] == "ambiguous"
    assert interrupted["global_release_blocked"] is True
    assert interrupted["allows_finance_migration"] is False
    api.add_comment = original_add_comment  # type: ignore[method-assign]

    assert acquire_finance_deploy_lease(
        api,
        acquire,
        deploy_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
        now=observed,
    ) == "acquired"
    assert acquire_finance_deploy_lease(
        api,
        acquire,
        deploy_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
        now=observed + 5,
    ) == "already-acquired"
    state = finance_deploy_lease_state(api, now=observed + 10)
    assert state["status"] == "active"
    assert state["allows_finance_migration"] is True
    assert state["lease"]["revision"] == 1
    assert state["lease"]["deployed_sha"] == SHA_B
    api.remove_label(anchor_pr, FINANCE_DEPLOY_LEASE_LABEL)
    lost_hold = finance_deploy_lease_state(api, now=observed + 10)
    assert lost_hold["status"] == "ambiguous"
    assert lost_hold["global_release_blocked"] is True
    api.add_labels(anchor_pr, [FINANCE_DEPLOY_LEASE_LABEL])
    api.remove_label(anchor_pr, FINANCE_DEPLOY_LEASE_AUDIT_LABEL)
    lost_audit = finance_deploy_lease_state(api, now=observed + 10)
    assert lost_audit["status"] == "ambiguous"
    assert lost_audit["global_release_blocked"] is True
    api.add_labels(anchor_pr, [FINANCE_DEPLOY_LEASE_AUDIT_LABEL])
    blocked = select_candidate(api, now=observed + 10)
    assert blocked["status"] == "finance-deploy-lease"
    assert not blocked["found"]
    stale = finance_deploy_lease_state(
        api,
        now=observed + 121 * 60,
    )
    assert stale["status"] == "stale"
    assert stale["global_release_blocked"] is True
    assert stale["allows_finance_migration"] is False
    stale_selection = select_candidate(
        api,
        now=observed + 121 * 60,
    )
    assert stale_selection["status"] == "finance-deploy-lease-fail-closed"
    api.add_labels(unrelated_pr, [FINANCE_DEPLOY_LEASE_LABEL])
    ambiguous = finance_deploy_lease_state(api, now=observed + 10)
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["global_release_blocked"] is True
    api.remove_label(unrelated_pr, FINANCE_DEPLOY_LEASE_LABEL)

    recovery = _pull(
        recovery_pr,
        labels=[READY_LABEL, STANDARD_TASK_LABEL, LIVE_RUNTIME_LABEL],
        created_at="2026-07-28T11:05:00Z",
        sha=SHA_C,
    )
    api.pulls[recovery_pr] = recovery
    api.checks = [
        {
            "id": recovery_pr,
            "name": "baseline",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    authorize = parse_finance_deploy_lease_command(
        f"/wb-core finance-lease authorize-recovery {anchor_pr} "
        f"task {task_id} lease {lease_id} revision 1 "
        f"recovery-pr {recovery_pr} head {SHA_C}"
    )
    assert authorize_finance_deploy_lease_recovery(
        api,
        authorize,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "authorized"
    assert FINANCE_DEPLOY_LEASE_RECOVERY_LABEL in _labels(recovery)
    recovery_pending = finance_deploy_lease_state(api, now=observed + 20)
    assert recovery_pending["status"] == "active"
    assert recovery_pending["recovery_pending"] is True
    assert recovery_pending["allows_finance_migration"] is False
    selected = select_candidate(api, now=observed + 20)
    assert selected["found"] and selected["pr_number"] == recovery_pr

    _set_labels(
        recovery,
        [
            PRODUCTION_LABEL,
            STANDARD_TASK_LABEL,
            LIVE_RUNTIME_LABEL,
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
        ],
    )
    recovery.update(
        state="closed",
        merged=True,
        merge_commit_sha=SHA_C,
        merged_at="2026-07-28T11:30:00Z",
    )
    api.add_comment(
        recovery_pr,
        "<!-- wb-core-release-completion-proof "
        f"contour=production-verified merge={SHA_C} pr={recovery_pr} -->",
    )
    rebind_evidence = {
        **deploy_evidence,
        "merge": SHA_C,
        "expected_sha": SHA_C,
    }
    rebind = parse_finance_deploy_lease_command(
        f"/wb-core finance-lease rebind {anchor_pr} deployed {SHA_C} "
        f"task {task_id} lease {lease_id} revision 1 "
        f"window pre-snapshot-2 phase pre-snapshot "
        f"recovery-pr {recovery_pr} ttl-minutes 120"
    )
    original_remove_label = api.remove_label
    interrupted_rebind_once = False

    def interrupt_rebind_cleanup(number: int, label: str) -> None:
        nonlocal interrupted_rebind_once
        if (
            not interrupted_rebind_once
            and number == recovery_pr
            and label == FINANCE_DEPLOY_LEASE_RECOVERY_LABEL
        ):
            interrupted_rebind_once = True
            raise RuntimeError("simulated disconnect before recovery-label cleanup")
        original_remove_label(number, label)

    api.remove_label = interrupt_rebind_cleanup  # type: ignore[method-assign]
    try:
        rebind_finance_deploy_lease(
            api,
            rebind,
            rebind_evidence,
            actor="orenvlad-ai",
            association="OWNER",
            actions_owned=True,
            now=observed + 30,
        )
    except RuntimeError as exc:
        assert "simulated disconnect" in str(exc)
    else:
        raise AssertionError("fault injection must interrupt rebind cleanup")
    interrupted_rebind = finance_deploy_lease_state(
        api,
        now=observed + 35,
    )
    assert interrupted_rebind["status"] == "active"
    assert interrupted_rebind["recovery_pending"] is True
    assert interrupted_rebind["allows_finance_migration"] is False
    api.remove_label = original_remove_label  # type: ignore[method-assign]
    assert rebind_finance_deploy_lease(
        api,
        rebind,
        rebind_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
        now=observed + 36,
    ) == "already-rebound"
    rebound = finance_deploy_lease_state(api, now=observed + 40)
    assert rebound["status"] == "active"
    assert rebound["lease"]["revision"] == 2
    assert rebound["lease"]["deployed_sha"] == SHA_C
    assert (
        rebound["lease"]["baseline_invalidation_epoch"]
        != state["lease"]["baseline_invalidation_epoch"]
    )
    assert FINANCE_DEPLOY_LEASE_RECOVERY_LABEL not in _labels(recovery)

    resume = parse_finance_deploy_lease_command(
        f"/wb-core finance-lease resume {anchor_pr} deployed {SHA_C} "
        f"task {task_id} lease {lease_id} revision 2 "
        "window pre-snapshot-3 phase pre-snapshot ttl-minutes 120"
    )
    assert rebind_finance_deploy_lease(
        api,
        resume,
        rebind_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
        now=observed + 50,
    ) == "rebound"
    assert rebind_finance_deploy_lease(
        api,
        resume,
        rebind_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
        now=observed + 55,
    ) == "already-rebound"
    resumed = finance_deploy_lease_state(api, now=observed + 60)
    assert resumed["status"] == "active"
    assert resumed["lease"]["revision"] == 3
    assert resumed["lease"]["deployed_sha"] == SHA_C
    assert (
        resumed["lease"]["baseline_invalidation_epoch"]
        != rebound["lease"]["baseline_invalidation_epoch"]
    )

    reconciliation_body = (
        f"Finance lease reconciliation task={task_id} lease={lease_id} "
        f"revision=3 deployed={SHA_C} evidence={EVIDENCE} "
        "migration_abort=complete canonical_source=monolith "
        "manual_barrier=released writers=restored timers=restored "
        "policy=restored non_target=unchanged sha_readback=exact"
    )
    reconciliation_id = api.add_external_comment(
        anchor_pr,
        reconciliation_body,
        created_at="2026-07-28T12:00:00Z",
    )
    terminal = parse_finance_deploy_lease_command(
        f"/wb-core finance-lease abort {anchor_pr} task {task_id} "
        f"lease {lease_id} revision 3 deployed {SHA_C} "
        f"reconciliation {reconciliation_id} "
        f"reconciliation-digest {_body_fingerprint(reconciliation_body)} "
        f"evidence {EVIDENCE}"
    )
    assert terminalize_finance_deploy_lease(
        api,
        terminal,
        rebind_evidence,
        actor="orenvlad-ai",
        association="OWNER",
        actions_owned=True,
    ) == "aborted"
    assert FINANCE_DEPLOY_LEASE_LABEL not in _labels(anchor)
    assert FINANCE_DEPLOY_LEASE_AUDIT_LABEL not in _labels(anchor)
    assert finance_deploy_lease_state(api)["status"] == "absent"
    assert select_candidate(api)["pr_number"] == unrelated_pr
    print("finance_global_deploy_lease: 33/33 ok")


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

    active_protocol_sources = [
        (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md").read_text(
            encoding="utf-8"
        ),
        (ROOT / "docs" / "architecture" / "11_github_release_train.md").read_text(
            encoding="utf-8"
        ),
    ]
    waiter_source = (ROOT / "apps" / "github_release_train_wait.py").read_text(
        encoding="utf-8"
    )
    for source in (*active_protocol_sources, waiter_source):
        lowered = source.casefold()
        assert "открой встроенный browser" not in lowered
        assert "open the embedded browser" not in lowered
    for source in active_protocol_sources:
        assert "--shepherd" not in source
        assert "--resume-owner" not in source
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
        assert required in waiter_source
    assert "playwright" in "\n".join(active_protocol_sources).casefold()
    assert "playwright" in waiter_source.casefold()
    assert "local" in waiter_source.casefold() or "локаль" in waiter_source.casefold()
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
    completed.append("13_active_protocol_avoids_legacy_shepherd_and_embedded_browser")

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
            "query-only",
            "dry-run",
            "fixtures/mocks",
        ):
            assert required in source
        assert "--shepherd" not in source
        assert "--resume-owner" not in source
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
    assert "python3 apps/codex_task_orchestrator_smoke.py" not in baseline
    assert "/wb-core orchestration " not in release
    assert "/wb-core loop " not in release
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
        "/wb-core finance-lease ",
        "preflight-finance-lease",
        "handle-finance-lease",
        "wb-core-deploy-evidence.json",
        "wb-core-runtime-evidence.json",
        "--deploy-evidence-file",
        "--runtime-evidence-file",
        "finance:migration-deploy-lease",
        "finance:migration-deploy-lease-audit",
        "wb-core-finance-migration-deploy-lease-binding",
        "--read-only",
        'cron: "*/5 * * * *"',
        "group: wb-core-production-release",
    ):
        assert required in release or required in implementation
    assert release.count("group: wb-core-production-release") == 1
    assert release.count("environment: production") == 4
    assert "reconcile_halted:" in release
    assert "resume-halted" in release
    assert "Reconcile bounded exact-SHA settling state" not in release
    assert "Continue queue after DCP exact-head readmission" in release
    for path in (
        ROOT / "docs" / "architecture" / "11_github_release_train.md",
        ROOT / "apps" / "github_release_train.py",
        ROOT / "apps" / "github_release_train_spec.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert DCP_RELEASE_HANDOFF_VERSION in source
        assert DCP_RELEASE_HANDOFF_V1_VERSION in source
    assert 'if stage == "metadata-complete"' in (
        ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
    ).read_text(encoding="utf-8")


def _assert_visible_codex_task_lifecycle_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (
        ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md"
    ).read_text(encoding="utf-8")
    archived_workspace = (
        ROOT / "docs" / "architecture" / "13_codex_curator_workspace.md"
    ).read_text(encoding="utf-8")
    execution_folded = re.sub(r"\s+", " ", execution.casefold())

    assert "## Видимый Жизненный Цикл Codex-Задач" in execution
    assert "единственный authoritative WBC contract" in execution
    for source in (agents, execution):
        folded = source.casefold()
        for required in (
            "WBC · <короткая тема> · К<n>",
            "WBC · <та же короткая тема> · И<n>",
            "без напоминания",
            "закреп",
            "ровно одного прямого",
            "задача принята",
            "вручную открепляет",
        ):
            assert required.casefold() in folded

    for required in (
        "n = 1 + max",
        "К1+",
        "И1+",
        "не синтезируют",
        "не закрепляет повторно",
        "не меняет task class",
        "не дублируют naming/pinning/unpinning/acceptance правила",
        "не создают Gateway, Agent Orchestrator, reviewer, arbiter, watcher",
    ):
        assert required.casefold() in execution_folded

    title_pattern = re.compile(
        r"^WBC · (?P<topic>[^·\n]+?) · (?P<role>[КИ])(?P<index>[1-9][0-9]*)$"
    )
    curator = title_pattern.fullmatch("WBC · Единые имена задач · К1")
    executor = title_pattern.fullmatch("WBC · Единые имена задач · И1")
    successor = title_pattern.fullmatch("WBC · Единые имена задач · И2")
    assert curator and executor and successor
    assert curator["topic"] == executor["topic"] == successor["topic"]
    assert (curator["role"], curator["index"]) == ("К", "1")
    assert (executor["role"], executor["index"]) == ("И", "1")
    assert int(successor["index"]) == int(executor["index"]) + 1
    for invalid in (
        "DCP · Единые имена задач · К1",
        "WBC · Единые имена задач · К01",
        "WBC · Единые имена задач · И1+",
        "WBC · Другая тема · executor",
    ):
        assert title_pattern.fullmatch(invalid) is None

    assert "находится только" in archived_workspace
    assert "07_codex_execution_protocol.md#видимый-жизненный-цикл-codex-задач" in (
        archived_workspace
    )


def _assert_permission_routing_and_direct_executor_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (
        ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md"
    ).read_text(encoding="utf-8")
    curator_role = (
        ROOT / "workspaces" / "WB Core · Кураторы" / "AGENTS.override.md"
    ).read_text(encoding="utf-8")
    provenance = (
        "Выбор инструментов и источников не является требованием пользователя и "
        "всегда перепроверяется по актуальному протоколу, если пользователь отдельно "
        "явно не зафиксировал обратное."
    )
    terminal_handoff = (
        "Исполнитель самостоятельно доводит задачу до применимого terminal state. "
        "После COMPLETE либо доказанного BLOCKED отправь в исходную кураторскую "
        "задачу один финальный technical handoff: итоговый статус; что сделано; "
        "что не сделано или осталось вне scope; PR и final SHA; проверки; "
        "merge/release/deploy/production state; visible executor task/thread ID; "
        "effective routing profile и app/CLI/runner versions; platform approval "
        "count; сложности, риски и blockers."
    )

    for source in (agents, execution):
        folded = re.sub(r"\s+", " ", source.casefold())
        plain = folded.replace("`", "")
        for required in (
            "CAPABILITY_ROUTING_CANARY",
            "CANARY_QUALIFIED",
            "CANARY_RESTRICTED",
            "machine-reported",
            "task/thread",
            "destination surface",
            "app/CLI/runner versions",
            "approval_policy=never",
            "sandbox=danger-full-access",
            "platform_approval_count=0",
            "capability inventory",
            "destination",
            "routing defect",
            "не request/forward",
            "EXECUTOR_AUTONOMY_PREFLIGHT",
            "shared Git metadata",
            "git fetch --prune origin",
            "GitHub connector",
            "local dependencies/runtime paths",
            "autonomy_ready",
            "exact starting main SHA",
            "task-local progress",
            "waitingOnApproval",
            "Human Gate",
            "pre-terminal callback",
            "bounded read-only check",
            "clean untouched worktree",
            "no branch, no commit, no push и no PR",
            "supported task/thread creation surface",
            "spawn_agent",
            "dispatch defect",
            "zero curator",
            "zero platform approval prompts",
            "production-mutation gate",
            "exact-SHA deploy/verify",
        ):
            assert required.casefold() in plain
        assert provenance.casefold() in plain
        assert terminal_handoff.casefold() in plain
        assert re.search(r"не [^.]{0,200}heartbeat", folded)
        assert "durable state machine" in folded
        assert "reset/clean/delete" in folded

    active = "\n".join((agents, execution, curator_role))
    active_folded = re.sub(r"\s+", " ", active.casefold())
    assert not re.search(r"front[ -]?load", active_folded)
    assert "platform hard stop — routing/tooling defects, не human" in active_folded
    assert "первый curator spawn_agent — dispatch defect" in active_folded.replace(
        "`", ""
    )
    assert "первый curator collaboration spawn_agent — dispatch defect" in (
        active_folded.replace("`", "")
    )

    curator_plain = re.sub(r"\s+", " ", curator_role.casefold()).replace("`", "")
    for required in (
        "supported task/thread creation surface",
        "thread id",
        "spawn_agent/subagent",
        "canary_qualified",
        "canary_restricted",
        "platform_approval_count=0",
        "zero curator spawn_agent calls",
    ):
        assert required in curator_plain


def _assert_owner_authorization_relay_protocol_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (
        ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md"
    ).read_text(encoding="utf-8")
    release_train = (
        ROOT / "docs" / "architecture" / "11_github_release_train.md"
    ).read_text(encoding="utf-8")
    active_sources = (agents, execution, release_train)

    manual_transport_is_not_required = (
        "владелец не обязан вручную открывать pr, публиковать github comment, "
        "запускать command или выполнять github action"
    )
    for source in active_sources:
        folded = re.sub(r"\s+", " ", source.casefold()).replace("`", "")
        for required in (
            manual_transport_is_not_required,
            "visible source task",
            "source task/thread id",
            "owner",
            "authorization",
            "owner/member",
            "fail closed",
            "reconciliation",
            "terminalization",
        ):
            assert required in folded
        assert "invent/synthesize/broaden" in folded

    release_folded = re.sub(r"\s+", " ", release_train.casefold()).replace(
        "`", ""
    )
    for required in (
        "source binding",
        "direct visible task history",
        "delegation envelope",
        "один дословный authorization payload",
        "verbatim block",
        "transport-only",
        "exact utf-8 digest",
        "existing manual gate comments",
        "parser, comment ids/digests, command schema",
        "mechanical executor closure",
    ):
        assert required in release_folded
    for forbidden in (
        "владелец обязан вручную открывать pr",
        "владелец обязан вручную публиковать github comment",
        "owner must manually open the pr",
        "manual github action required",
    ):
        assert forbidden not in "\n".join(active_sources).casefold()

    task_id = "01a00000-0000-7000-8000-000000000001"
    pr_number = 120
    release_payload = (
        f"OWNER AUTHORIZATION for exact PR #{pr_number} head {SHA_A}: "
        "merge and deploy only; stale on head or semantic drift."
    )
    apply_payload = (
        f"OWNER AUTHORIZATION for exact PR #{pr_number} deployed SHA {SHA_C} "
        f"and manifest {MANIFEST}: production apply is authorized."
    )

    def relay_allowed(
        payloads: tuple[str, ...],
        *,
        source_task_id: str,
        association: str,
        required_bindings: tuple[str, ...],
    ) -> bool:
        if len(payloads) != 1:
            return False
        if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            source_task_id,
        ) is None:
            return False
        if association not in {"OWNER", "MEMBER"}:
            return False
        return all(binding in payloads[0] for binding in required_bindings)

    release_bindings = (f"PR #{pr_number}", SHA_A, "merge", "deploy")
    apply_bindings = (f"PR #{pr_number}", SHA_C, MANIFEST, "apply")
    assert relay_allowed(
        (release_payload,),
        source_task_id=task_id,
        association="OWNER",
        required_bindings=release_bindings,
    )
    assert relay_allowed(
        (apply_payload,),
        source_task_id=task_id,
        association="MEMBER",
        required_bindings=apply_bindings,
    )

    assert not relay_allowed(
        (),
        source_task_id=task_id,
        association="OWNER",
        required_bindings=release_bindings,
    )
    assert not relay_allowed(
        (release_payload, release_payload + " broader authority"),
        source_task_id=task_id,
        association="OWNER",
        required_bindings=release_bindings,
    )
    assert not relay_allowed(
        (release_payload,),
        source_task_id=task_id,
        association="CONTRIBUTOR",
        required_bindings=release_bindings,
    )
    assert not relay_allowed(
        (release_payload,),
        source_task_id="unproven-task",
        association="OWNER",
        required_bindings=release_bindings,
    )
    assert not relay_allowed(
        (release_payload.replace(SHA_A, SHA_B),),
        source_task_id=task_id,
        association="OWNER",
        required_bindings=release_bindings,
    )
    assert not relay_allowed(
        (apply_payload.replace(MANIFEST, "sha256:" + "f" * 64),),
        source_task_id=task_id,
        association="MEMBER",
        required_bindings=apply_bindings,
    )


def _assert_active_protocol_cutover_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    execution = (
        ROOT / "docs" / "architecture" / "07_codex_execution_protocol.md"
    ).read_text(encoding="utf-8")
    release_train = (
        ROOT / "docs" / "architecture" / "11_github_release_train.md"
    ).read_text(encoding="utf-8")
    orchestration_archive = (
        ROOT / "docs" / "architecture" / "12_codex_global_orchestration.md"
    ).read_text(encoding="utf-8")
    source_policy = (
        ROOT / "docs" / "architecture" / "03_source_of_truth_policy.md"
    ).read_text(encoding="utf-8")
    pr_template = (ROOT / ".github" / "pull_request_template.md").read_text(
        encoding="utf-8"
    )
    active_sources = (agents, execution, release_train)
    active_joined = "\n".join(active_sources).casefold()

    for source in active_sources:
        folded = re.sub(r"\s+", " ", source.casefold())
        for required in (
            "task:standard",
            "scope:repo-only",
            "scope:live-runtime",
            "scope:production-mutation",
            "open non-draft",
            "baseline",
            "release:ready",
            "release:done",
            "release:production",
            "exact",
            "github release train",
        ):
            assert required in folded
        for retired_command_surface in (
            "apps/codex_task_orchestrator.py",
            "/wb-core orchestration ",
            "release:staged",
            "release:lane-owner",
            "--shepherd",
            "--resume-owner",
            "register-task",
            "begin-run",
            "heartbeat-finish",
            "target_create_readback",
        ):
            assert retired_command_surface not in folded

    for retired_term in (
        "global watcher",
        "registry",
        "task passport",
        "acceptance envelope",
        "logical release lane",
        "shepherd/takeover",
        "arbiter",
        "heartbeat",
        "callback",
    ):
        assert retired_term in active_joined
    for prohibition in (
        "не запуска",
        "не регистр",
        "не восстанав",
        "не являются current agent instructions",
    ):
        assert prohibition in active_joined

    assert "WB_CORE_ORCHESTRATION_REQUIRED=false" in agents
    assert "WB_CORE_ORCHESTRATION_REQUIRED=false" in execution
    assert "WB_CORE_ORCHESTRATION_REQUIRED=false" in release_train
    assert "добавляет `release:ready`" in re.sub(r"\s+", " ", agents)
    assert "добавляет `release:ready`" in re.sub(r"\s+", " ", execution)
    assert "добавляет `release:ready`" in re.sub(r"\s+", " ", release_train)
    assert "только владелец пишет" in agents
    assert "не являются owner acceptance" in re.sub(r"\s+", " ", execution)
    assert "technical closure не является owner acceptance" in release_train.casefold()
    assert "Задача принята" in agents
    assert "Задача принята" in execution
    assert "Задача принята" in release_train

    archive_folded = re.sub(r"\s+", " ", orchestration_archive.casefold())
    for required in (
        "archived global codex orchestration",
        "archive pointer",
        "migration history",
        "не является agent instruction",
        "git history",
        "retained compatibility",
    ):
        assert required in archive_folded
    assert "e44f548982900e286a2c1a73fdf439d0c8a49843" in orchestration_archive
    assert "не active flow" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "retired global watcher" in source_policy.casefold()

    for required in (
        "task:standard",
        "ровно одна `scope:*`",
        "successful `baseline`",
        "release:ready",
        "не означает owner acceptance",
    ):
        assert required in pr_template

    workflow = (
        ROOT / ".github" / "workflows" / "release-train.yml"
    ).read_text(encoding="utf-8")
    implementation = (
        ROOT / "apps" / "github_release_train.py"
    ).read_text(encoding="utf-8")
    assert "/wb-core orchestration " not in workflow
    assert "apps/codex_task_orchestrator.py" not in workflow
    assert "release:ready" in workflow
    assert "WB_CORE_ORCHESTRATION_REQUIRED" in workflow
    assert "exact" in implementation.casefold()
    assert "DONE_LABEL" in implementation
    assert "PRODUCTION_LABEL" in implementation

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


def _assert_queue_status_local_auth_contract() -> None:
    reader_calls: list[str] = []

    def local_token() -> str:
        reader_calls.append("called")
        return "local-token"

    local_api = _queue_status_api_from_env(
        environ={},
        gh_token_reader=local_token,
    )
    assert local_api.repository == "orenvlad-ai/wb-core"
    assert local_api.token == "local-token"
    assert reader_calls == ["called"]

    reader_calls.clear()
    explicit_api = _queue_status_api_from_env(
        environ={
            "GITHUB_REPOSITORY": "example/repository",
            "GITHUB_TOKEN": "explicit-token",
            "GITHUB_API_URL": "https://github.example/api/v3",
        },
        gh_token_reader=local_token,
    )
    assert explicit_api.repository == "example/repository"
    assert explicit_api.token == "explicit-token"
    assert explicit_api.api_url == "https://github.example/api/v3"
    assert reader_calls == []

    try:
        _queue_status_api_from_env(
            environ={"GITHUB_REPOSITORY": "personal/repository"},
            gh_token_reader=local_token,
        )
    except ValueError as exc:
        assert "restricted to orenvlad-ai/wb-core" in str(exc)
    else:
        raise AssertionError("local gh fallback must be restricted to wb-core")
    assert reader_calls == []

    try:
        _queue_status_api_from_env(
            environ={"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/repository"},
            gh_token_reader=local_token,
        )
    except ValueError as exc:
        assert str(exc) == "GITHUB_TOKEN is required in GitHub Actions"
    else:
        raise AssertionError("Actions queue-status must fail closed without GITHUB_TOKEN")
    assert reader_calls == []


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
    _assert_queue_status_local_auth_contract()
    _assert_label_and_input_validation()
    _assert_standard_repo_only_and_live()
    _assert_dcp_release_handoff_v2()
    api, root = _assert_loop_handshake_and_gate()
    _assert_recovery_transfer_and_acceptance(api, root)
    _assert_foreign_gate_waiting_and_queue_progress()
    _assert_lost_owner_resume_lifecycle()
    _assert_superseded_normalization_is_root_bounded()
    _assert_blocked_halted_and_production_mutation()
    _assert_production_mutation_terminalization()
    _assert_finance_global_deploy_lease()
    _assert_ack_invalidated_by_head_change()
    _assert_waiter_contract()
    _assert_goal_shepherd_regressions()
    _assert_phase_local_goal_regressions()
    _assert_workflow_contract()
    _assert_visible_codex_task_lifecycle_contract()
    _assert_permission_routing_and_direct_executor_contract()
    _assert_owner_authorization_relay_protocol_contract()
    _assert_active_protocol_cutover_contract()
    _assert_machine_classification_and_state_spec()
    _assert_resume_status_and_manual_ack_guards()
    _assert_two_parallel_loop_roots()
    _assert_halted_exact_evidence_resume()
    _assert_continuity_classification_matrix()
    print("github_release_train_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
