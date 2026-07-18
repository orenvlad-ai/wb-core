#!/usr/bin/env python3
"""Codex CLI waiter for wb-core Release Train states.

Polling is read-only.  For LOOP tasks the default mode emits exactly one
GitHub-native acknowledgement comment when ``release:awaiting-agent`` is
observed, then continues polling until the UI handoff or a terminal state.
Normal queue ownership by another LOOP is durable waiting and never becomes
a blocker because of elapsed time or repeated observations.
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
    GitHubApi,
    HALTED_LABEL,
    LIVE_RUNTIME_LABEL,
    LOOP_TASK_LABEL,
    PRODUCTION_LABEL,
    REPO_ONLY_LABEL,
    STANDARD_TASK_LABEL,
    label_names,
    loop_ack_label,
    loop_root_from_labels,
    queue_gate_state,
    release_state_from_labels,
    scope_from_labels,
    task_class_from_labels,
)


EXIT_BLOCKED = 2
EXIT_AWAITING_UI = 3
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
        queue_status in {"awaiting-agent", "awaiting-ui"}
        and queue_pr != pr_number
        and not (queue_status == "awaiting-ui" and target_root == queue_root and queue_root > 0)
    )
    if task_class == LOOP_TASK_LABEL and scope != LIVE_RUNTIME_LABEL:
        raise ValueError("LOOP waiter requires scope:live-runtime")
    if state in {BLOCKED_LABEL, HALTED_LABEL}:
        return {
            "action": "blocked",
            "task_class": task_class,
            "scope": scope,
            "state": state,
            "reason": f"PR #{pr_number} is in its own {state} state",
        }
    if task_class == LOOP_TASK_LABEL:
        if state == AWAITING_AGENT_LABEL:
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
    if queue_status == "halted":
        decision["action"] = "blocked"
        decision["reason"] = f"global release:halted gate is held by PR #{queue_pr}"
    elif queue_status == "gate-conflict":
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
    emit: Callable[[str], None] = print,
) -> int:
    next_status = time.monotonic() + status_seconds if status_seconds > 0 else None
    previous: tuple[str, str, str, str, str, int] | None = None
    acknowledged_heads: set[str] = set()
    while True:
        queue = queue_gate_state(api)
        pull = api.get_pull(pr_number)
        labels = label_names(pull)
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
        if snapshot != previous:
            emit(
                f"PR #{pr_number} class={decision['task_class']} scope={decision['scope']} "
                f"state={decision['state']} head={head_sha} "
                f"queue={queue.get('status', 'idle')} gate_pr={queue.get('pr_number', '')}"
            )
            if decision.get("reason"):
                emit(f"PR #{pr_number} {decision['reason']}")
            previous = snapshot
        action = decision["action"]
        if action == "success":
            return 0
        if action == "blocked":
            if decision.get("reason"):
                emit(f"PR #{pr_number} fail-closed: {decision['reason']}")
            return EXIT_BLOCKED
        if action == "awaiting-ui":
            return EXIT_AWAITING_UI
        if action == "ack-agent" and acknowledge_agent and head_sha not in acknowledged_heads:
            try:
                loop_ack_label(head_sha)
            except ValueError as exc:
                emit(f"PR #{pr_number} fail-closed: {exc}")
                return EXIT_BLOCKED
            command = f"/wb-core loop ack-agent {pr_number} head {head_sha}"
            api.add_comment(pr_number, command)
            acknowledged_heads.add(head_sha)
            emit(f"PR #{pr_number} acknowledgement submitted for exact head {head_sha}")
        if next_status is not None and time.monotonic() >= next_status:
            emit(
                f"PR #{pr_number} is still in a normal non-terminal queue state; "
                "elapsed time does not make it blocked, polling continues"
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
        default=7200,
        help="emit a waiting heartbeat at this interval; never terminates normal queue waiting",
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
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "pr_number": args.pr}), file=sys.stderr)
        return EXIT_INTERRUPTED
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
