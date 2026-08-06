"""Bounded read-only waiter for the direct GitHub Release Train."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any, Callable, Mapping


TERMINAL_SUCCESS = {"release:done", "release:production"}
ACTIVE = {"release:ready", "release:running"}
PRESERVED_TERMINAL = {"release:superseded", "release:retired"}

EXIT_SUCCESS = 0
EXIT_EXTERNAL_BLOCKER = 2
EXIT_OWN_ACTION = 5
EXIT_CONTINUE_WAITING = 6
EXIT_TERMINAL_FAILURE = 7


def _run_gh(pr: int) -> Mapping[str, Any]:
    process = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--json",
            "number,state,isDraft,headRefOid,labels,mergeCommit,url,comments",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "gh pr view failed")
    payload = json.loads(process.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("gh pr view returned a non-object payload")
    return payload


def _labels(payload: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in payload.get("labels") or []
        if isinstance(item, Mapping)
    }


def _repo_owned_comment_bodies(payload: Mapping[str, Any]) -> list[str]:
    bodies: list[str] = []
    for item in payload.get("comments") or []:
        if not isinstance(item, Mapping):
            continue
        author = item.get("author") or item.get("user") or {}
        login = str(author.get("login") or "") if isinstance(author, Mapping) else ""
        if login in {"github-actions", "github-actions[bot]"}:
            bodies.append(str(item.get("body") or ""))
    return bodies


def _terminal_proof(payload: Mapping[str, Any], state: str) -> bool:
    if str(payload.get("state") or "").upper() != "MERGED":
        return False
    number = int(payload.get("number") or 0)
    merge = payload.get("mergeCommit") or {}
    merge_sha = str(merge.get("oid") or "").lower() if isinstance(merge, Mapping) else ""
    labels = _labels(payload)
    if number <= 0 or len(merge_sha) != 40 or "task:standard" not in labels:
        return False
    scopes = labels & {"scope:repo-only", "scope:live-runtime", "scope:production-mutation"}
    if len(scopes) != 1:
        return False
    scope = next(iter(scopes))
    expected_scope = "scope:repo-only" if state == "release:done" else "scope:live-runtime"
    if scope != expected_scope:
        return False
    contour = "repo-only" if state == "release:done" else "production-verified"
    completion = (
        "<!-- wb-core-release-completion-proof "
        f"contour={contour} merge={merge_sha} pr={number} -->"
    )
    reconciliation = (
        f"<!-- wb-core-release-reconcile-proof merge={merge_sha} pr={number} -->"
    )
    bodies = _repo_owned_comment_bodies(payload)
    return any(completion in body for body in bodies) or (
        state == "release:production"
        and any(reconciliation in body for body in bodies)
    )


def evaluate_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    labels = _labels(payload)
    success = sorted(labels & TERMINAL_SUCCESS)
    active = labels & ACTIVE
    if len(success) == 1:
        if not _terminal_proof(payload, success[0]):
            return {
                "disposition": "OWN_ACTION",
                "exit_code": EXIT_OWN_ACTION,
                "release_state": "missing-terminal-proof",
                "allowed_next_action": "read Release Train checks and reconcile exact merge proof",
                "owner_accepted": False,
            }
        return {
            "disposition": "TERMINAL_SUCCESS",
            "exit_code": EXIT_SUCCESS,
            "release_state": success[0],
            "allowed_next_action": "prepare manual owner-acceptance handoff",
            "owner_accepted": False,
        }
    recoverable = labels & {"release:blocked", "release:halted"}
    if recoverable:
        state = "release:halted" if "release:halted" in recoverable else "release:blocked"
        return {
            "disposition": "OWN_ACTION",
            "exit_code": EXIT_OWN_ACTION,
            "release_state": state,
            "allowed_next_action": (
                "fix and enqueue a fresh exact head"
                if state == "release:blocked"
                else "run exact-SHA repo-owned reconciliation"
            ),
            "owner_accepted": False,
        }
    if active:
        state = "release:running" if "release:running" in active else "release:ready"
        return {
            "disposition": "CONTINUE_WAITING",
            "exit_code": EXIT_CONTINUE_WAITING,
            "release_state": state,
            "allowed_next_action": "wait for the serialized Release Train",
            "owner_accepted": False,
        }
    preserved = sorted(labels & PRESERVED_TERMINAL)
    github_state = str(payload.get("state") or "").upper()
    if preserved:
        return {
            "disposition": "TERMINAL_INELIGIBLE",
            "exit_code": EXIT_TERMINAL_FAILURE,
            "release_state": preserved[0],
            "allowed_next_action": "none; preserved historical PR is not enqueueable",
            "owner_accepted": False,
        }
    if github_state == "MERGED":
        return {
            "disposition": "OWN_ACTION",
            "exit_code": EXIT_OWN_ACTION,
            "release_state": "missing-terminal-proof",
            "allowed_next_action": "read Release Train checks and reconcile terminal evidence",
            "owner_accepted": False,
        }
    if github_state != "OPEN" or bool(payload.get("isDraft")):
        return {
            "disposition": "TERMINAL_INELIGIBLE",
            "exit_code": EXIT_TERMINAL_FAILURE,
            "release_state": "closed-or-draft",
            "allowed_next_action": "open a non-draft PR with a fresh exact head",
            "owner_accepted": False,
        }
    return {
        "disposition": "OWN_ACTION",
        "exit_code": EXIT_OWN_ACTION,
        "release_state": "not-enqueued",
        "allowed_next_action": (
            f"after exact-head baseline, comment /wb-core release enqueue "
            f"{int(payload.get('number') or 0)} head {str(payload.get('headRefOid') or '')}"
        ),
        "owner_accepted": False,
    }


def wait_for_release(
    pr: int,
    *,
    once: bool = False,
    interval_seconds: float = 20.0,
    timeout_seconds: float = 0.0,
    reader: Callable[[int], Mapping[str, Any]] = _run_gh,
) -> dict[str, Any]:
    if pr <= 0:
        raise ValueError("PR number must be positive")
    if interval_seconds <= 0:
        raise ValueError("interval must be positive")
    started = time.monotonic()
    while True:
        payload = dict(reader(pr))
        result = evaluate_release(payload)
        result.update(
            {
                "pr": pr,
                "head_sha": str(payload.get("headRefOid") or ""),
                "url": str(payload.get("url") or ""),
            }
        )
        if once or int(result["exit_code"]) != EXIT_CONTINUE_WAITING:
            return result
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            result["reason"] = "bounded wait elapsed; release remains non-terminal"
            return result
        time.sleep(interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wait for direct wb-core Release Train state")
    parser.add_argument("pr", type=int)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = wait_for_release(
            args.pr,
            once=args.once,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return EXIT_EXTERNAL_BLOCKER
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
