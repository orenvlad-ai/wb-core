"""Bounded read-only waiter for the direct GitHub Release Train."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any, Callable, Mapping


TERMINAL_SUCCESS = {"release:done", "release:production"}
TERMINAL_FAILURE = {"release:blocked", "release:halted"}
ACTIVE = {"release:ready", "release:running"}

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
            "number,state,isDraft,headRefOid,labels,mergeCommit,url",
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


def evaluate_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    labels = _labels(payload)
    success = sorted(labels & TERMINAL_SUCCESS)
    failure = sorted(labels & TERMINAL_FAILURE)
    active = sorted(labels & ACTIVE)
    if len(success) == 1 and not failure:
        return {
            "disposition": "TERMINAL_SUCCESS",
            "exit_code": EXIT_SUCCESS,
            "release_state": success[0],
            "allowed_next_action": "prepare manual owner-acceptance handoff",
            "owner_accepted": False,
        }
    if failure:
        return {
            "disposition": "TERMINAL_FAILURE",
            "exit_code": EXIT_TERMINAL_FAILURE,
            "release_state": failure[0],
            "allowed_next_action": (
                "fix and enqueue a fresh exact head"
                if failure[0] == "release:blocked"
                else "run exact-SHA repo-owned reconciliation"
            ),
            "owner_accepted": False,
        }
    if active:
        return {
            "disposition": "CONTINUE_WAITING",
            "exit_code": EXIT_CONTINUE_WAITING,
            "release_state": active[0],
            "allowed_next_action": "wait for the serialized Release Train",
            "owner_accepted": False,
        }
    if str(payload.get("state") or "").upper() == "MERGED":
        return {
            "disposition": "OWN_ACTION",
            "exit_code": EXIT_OWN_ACTION,
            "release_state": "missing-terminal-proof",
            "allowed_next_action": "read Release Train checks and reconcile terminal evidence",
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
