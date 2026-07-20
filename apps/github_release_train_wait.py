#!/usr/bin/env python3
"""Codex CLI waiter for wb-core Release Train states.

The waiter maintains one idempotent owner/status comment.  For LOOP tasks it
also emits at most one exact-head acknowledgement command when requested.
Normal foreign queue ownership is durable waiting and never becomes a blocker
because of elapsed time or repeated observations.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_train import (  # noqa: E402
    AWAITING_AGENT_LABEL,
    AWAITING_UI_LABEL,
    BLOCKED_LABEL,
    DONE_LABEL,
    ensure_ca_bundle,
    GitHubApi,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    LOOP_TASK_LABEL,
    NEEDS_RESUME_LABEL,
    PRODUCTION_LABEL,
    READY_LABEL,
    REPO_ONLY_LABEL,
    RUNNING_LABEL,
    STANDARD_TASK_LABEL,
    SUPERSEDED_LABEL,
    ReleaseClassificationBlocked,
    label_names,
    loop_ack_label,
    loop_root_from_labels,
    loop_registration_kind,
    mark_classification_blocked,
    queue_gate_state,
    release_state_from_labels,
    scope_from_labels,
    task_class_from_labels,
    terminal_state_proven,
    upsert_status_comment,
)


EXIT_BLOCKED = 2
EXIT_AWAITING_UI = 3
EXIT_RESUMED = 4
EXIT_INTERRUPTED = 130


def evaluate_release(
    labels: set[str],
    *,
    pr_number: int = 0,
    queue: dict[str, object] | None = None,
) -> dict[str, str]:
    task_class = task_class_from_labels(labels)
    scope = scope_from_labels(labels)
    state = release_state_from_labels(labels)
    queue = queue or {"status": "idle"}
    queue_status = str(queue.get("status") or "idle")
    queue_pr = int(queue.get("pr_number") or 0)
    target_root = loop_root_from_labels(labels) if task_class == LOOP_TASK_LABEL else None
    queue_root = int(queue.get("loop_root") or 0)
    foreign_gate = (
        queue_status in {"ready", "running", "awaiting-agent", "awaiting-ui", "halted"}
        and queue_pr != pr_number
        and not (queue_status == "awaiting-ui" and target_root == queue_root and queue_root > 0)
    )
    if task_class == LOOP_TASK_LABEL and scope != LIVE_RUNTIME_LABEL:
        raise ValueError("LOOP waiter requires scope:live-runtime")
    if state == SUPERSEDED_LABEL:
        return {
            "action": "terminal-superseded",
            "task_class": task_class,
            "scope": scope,
            "state": state,
            "reason": f"PR #{pr_number} is terminal release:superseded",
        }
    if state in {BLOCKED_LABEL, HALTED_LABEL}:
        return {
            "action": "blocked",
            "task_class": task_class,
            "scope": scope,
            "state": state,
            "reason": f"PR #{pr_number} is in its own {state} state",
        }
    if task_class == LOOP_TASK_LABEL:
        if NEEDS_RESUME_LABEL in labels:
            action = "needs-resume"
        elif state == AWAITING_AGENT_LABEL:
            action = "ack-agent"
        elif state == AWAITING_UI_LABEL:
            action = "awaiting-ui"
        elif state == PRODUCTION_LABEL:
            action = "success"
        elif foreign_gate:
            action = "wait-foreign-gate"
        else:
            action = "wait"
        decision = {"action": action, "task_class": task_class, "scope": scope, "state": state}
    else:
        if task_class != STANDARD_TASK_LABEL:
            raise ValueError(f"unsupported task class: {task_class}")
        expected = DONE_LABEL if scope == REPO_ONLY_LABEL else PRODUCTION_LABEL
        if state == expected:
            action = "success"
        elif foreign_gate:
            action = "wait-foreign-gate"
        else:
            action = "wait"
        decision = {"action": action, "task_class": task_class, "scope": scope, "state": state}

    if decision["action"] == "success":
        return decision
    if queue_status == "gate-conflict":
        decision["action"] = "blocked"
        decision["reason"] = str(queue.get("reason") or "conflicting exclusive release gates")
    elif decision["action"] == "wait-foreign-gate":
        decision["reason"] = (
            f"foreign {queue_status} gate is held by PR #{queue_pr}; normal queue waiting continues"
        )
    return decision


def wait_for_release(
    api: GitHubApi,
    pr_number: int,
    *,
    status_seconds: float,
    poll_seconds: float,
    acknowledge_agent: bool,
    resume_owner: bool = False,
    owner: str = "codex-cli",
    emit: Callable[[str], None] = print,
) -> int:
    next_status = time.monotonic() + status_seconds if status_seconds > 0 else None
    previous: tuple[str, str, str, str, str, int] | None = None
    acknowledged_heads: set[str] = set()
    resume_submitted: set[str] = set()
    while True:
        queue = queue_gate_state(api)
        pull = api.get_pull(pr_number)
        labels = label_names(pull)
        task_class = task_class_from_labels(labels)
        state = release_state_from_labels(labels)
        if task_class == LOOP_TASK_LABEL and state in {
            READY_LABEL,
            RUNNING_LABEL,
            AWAITING_AGENT_LABEL,
            AWAITING_UI_LABEL,
        }:
            try:
                loop_registration_kind(api, pull)
            except ReleaseClassificationBlocked as exc:
                if state != AWAITING_UI_LABEL:
                    mark_classification_blocked(api, pr_number, exc)
                emit(
                    f"PR #{pr_number} fail-closed classification `{exc.code}`: {exc}"
                )
                return EXIT_BLOCKED
        decision = evaluate_release(labels, pr_number=pr_number, queue=queue)
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        snapshot = (
            decision["task_class"],
            decision["scope"],
            decision["state"],
            head_sha,
            str(queue.get("status") or "idle"),
            int(queue.get("pr_number") or 0),
        )
        changed = snapshot != previous
        if changed:
            emit(
                f"PR #{pr_number} class={decision['task_class']} scope={decision['scope']} "
                f"state={decision['state']} head={head_sha} "
                f"queue={queue.get('status', 'idle')} gate_pr={queue.get('pr_number', '')}"
            )
            if decision.get("reason"):
                emit(f"PR #{pr_number} {decision['reason']}")
            previous = snapshot
        action = decision["action"]
        if changed and action in {"wait", "wait-foreign-gate", "ack-agent"}:
            upsert_status_comment(
                api,
                pr_number,
                owner=owner,
                reason=str(decision.get("reason") or "normal deterministic queue waiting"),
                last_action="Codex CLI waiter started or observed a state change",
                intervention=False,
            )
        if action == "success":
            if not terminal_state_proven(api, pull):
                emit(
                    f"PR #{pr_number} fail-closed: terminal label lacks repo-owned exact-SHA proof"
                )
                return EXIT_BLOCKED
            return 0
        if action == "blocked":
            if decision.get("reason"):
                emit(f"PR #{pr_number} fail-closed: {decision['reason']}")
            return EXIT_BLOCKED
        if action == "terminal-superseded":
            emit(f"PR #{pr_number} terminal: release:superseded")
            return EXIT_BLOCKED
        if action == "awaiting-ui":
            return EXIT_AWAITING_UI
        if action == "needs-resume":
            if not resume_owner:
                emit(
                    f"PR #{pr_number} requires explicit owner resume; no acknowledgement was submitted"
                )
                return EXIT_RESUMED
            root = loop_root_from_labels(labels)
            if root is None:
                emit(f"PR #{pr_number} fail-closed: registered LOOP root is missing")
                return EXIT_BLOCKED
            command = (
                f"/wb-core loop resume-owner {pr_number} head {head_sha} root {root}"
            )
            if head_sha not in resume_submitted:
                if not any(
                    str(item.get("body") or "").strip() == command
                    for item in api.list_comments(pr_number)
                ):
                    api.add_comment(pr_number, command)
                resume_submitted.add(head_sha)
                emit(f"PR #{pr_number} owner resume submitted for exact head {head_sha}")
            return EXIT_RESUMED
        if action == "ack-agent" and acknowledge_agent and head_sha not in acknowledged_heads:
            try:
                loop_ack_label(head_sha)
            except ValueError as exc:
                emit(f"PR #{pr_number} fail-closed: {exc}")
                return EXIT_BLOCKED
            command = f"/wb-core loop ack-agent {pr_number} head {head_sha}"
            if not any(
                str(item.get("body") or "").strip() == command
                for item in api.list_comments(pr_number)
            ):
                api.add_comment(pr_number, command)
            acknowledged_heads.add(head_sha)
            emit(f"PR #{pr_number} acknowledgement submitted for exact head {head_sha}")
        if next_status is not None and time.monotonic() >= next_status:
            emit(
                f"PR #{pr_number} is still in a normal non-terminal queue state; "
                "elapsed time does not make it blocked, polling continues"
            )
            upsert_status_comment(
                api,
                pr_number,
                owner=owner,
                reason=str(decision.get("reason") or "normal deterministic queue waiting"),
                last_action="Codex CLI waiter heartbeat",
                intervention=False,
            )
            next_status = time.monotonic() + status_seconds
        time.sleep(poll_seconds)


def _run_gh(*arguments: str) -> str:
    result = subprocess.run(
        ["gh", *arguments],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _repository(explicit: str) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if configured:
        return configured
    return _run_gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")


def _token() -> str:
    configured = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    return configured or _run_gh("auth", "token")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wait for a wb-core Release Train PR")
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--repository", default="", help="owner/name; inferred from gh when omitted")
    parser.add_argument(
        "--status-seconds",
        "--timeout-seconds",
        dest="status_seconds",
        type=float,
        default=300,
        help="emit a waiting heartbeat at this interval; never terminates normal queue waiting",
    )
    parser.add_argument(
        "--resume-owner",
        action="store_true",
        help="claim release:needs-resume for this exact head/root without acknowledging",
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("WB_CORE_RELEASE_OWNER", "codex-cli"),
        help="stable owner identity recorded in the idempotent status comment",
    )
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument(
        "--no-ack-agent",
        action="store_true",
        help="do not submit the exact LOOP acknowledgement comment",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        ensure_ca_bundle()
        api = GitHubApi(
            repository=_repository(args.repository),
            token=_token(),
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        return wait_for_release(
            api,
            args.pr,
            status_seconds=args.status_seconds,
            poll_seconds=args.poll_seconds,
            acknowledge_agent=not args.no_ack_agent,
            resume_owner=args.resume_owner,
            owner=args.owner,
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "pr_number": args.pr}), file=sys.stderr)
        return EXIT_INTERRUPTED
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
