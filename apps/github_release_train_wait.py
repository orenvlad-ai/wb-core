#!/usr/bin/env python3
"""Codex CLI waiter for wb-core Release Train states.

Polling is read-only.  For LOOP tasks the default mode emits exactly one
GitHub-native acknowledgement comment when ``release:awaiting-agent`` is
observed, then continues polling until the UI handoff or a terminal state.
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
    release_state_from_labels,
    scope_from_labels,
    task_class_from_labels,
)


EXIT_BLOCKED = 2
EXIT_AWAITING_UI = 3
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130


def evaluate_release(labels: set[str]) -> dict[str, str]:
    task_class = task_class_from_labels(labels)
    scope = scope_from_labels(labels)
    state = release_state_from_labels(labels)
    if task_class == LOOP_TASK_LABEL and scope != LIVE_RUNTIME_LABEL:
        raise ValueError("LOOP waiter requires scope:live-runtime")
    if state in {BLOCKED_LABEL, HALTED_LABEL}:
        return {"action": "blocked", "task_class": task_class, "scope": scope, "state": state}
    if task_class == LOOP_TASK_LABEL:
        if state == AWAITING_AGENT_LABEL:
            action = "ack-agent"
        elif state == AWAITING_UI_LABEL:
            action = "awaiting-ui"
        elif state == PRODUCTION_LABEL:
            action = "success"
        else:
            action = "wait"
        return {"action": action, "task_class": task_class, "scope": scope, "state": state}
    if task_class != STANDARD_TASK_LABEL:
        raise ValueError(f"unsupported task class: {task_class}")
    expected = DONE_LABEL if scope == REPO_ONLY_LABEL else PRODUCTION_LABEL
    action = "success" if state == expected else "wait"
    return {"action": action, "task_class": task_class, "scope": scope, "state": state}


def wait_for_release(
    api: GitHubApi,
    pr_number: int,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    acknowledge_agent: bool,
    emit: Callable[[str], None] = print,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[str, str, str, str] | None = None
    acknowledged_heads: set[str] = set()
    while True:
        pull = api.get_pull(pr_number)
        labels = label_names(pull)
        decision = evaluate_release(labels)
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        snapshot = (
            decision["task_class"],
            decision["scope"],
            decision["state"],
            head_sha,
        )
        if snapshot != previous:
            emit(
                f"PR #{pr_number} class={decision['task_class']} scope={decision['scope']} "
                f"state={decision['state']} head={head_sha}"
            )
            previous = snapshot
        action = decision["action"]
        if action == "success":
            return 0
        if action == "blocked":
            return EXIT_BLOCKED
        if action == "awaiting-ui":
            return EXIT_AWAITING_UI
        if action == "ack-agent" and acknowledge_agent and head_sha not in acknowledged_heads:
            command = f"/wb-core loop ack-agent {pr_number} head {head_sha}"
            api.add_comment(pr_number, command)
            acknowledged_heads.add(head_sha)
            emit(f"PR #{pr_number} acknowledgement submitted for exact head {head_sha}")
        if time.monotonic() >= deadline:
            emit(f"PR #{pr_number} waiter timed out after {timeout_seconds:g}s")
            return EXIT_TIMEOUT
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
    parser.add_argument("--timeout-seconds", type=float, default=7200)
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
            timeout_seconds=args.timeout_seconds,
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
