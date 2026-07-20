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
from typing import Any, Callable, Mapping


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
    ReleaseBlocked,
    label_names,
    loop_ack_label,
    loop_root_label,
    loop_root_from_labels,
    loop_registration_kind,
    queue_gate_state,
    release_state_from_labels,
    scope_from_labels,
    task_class_from_labels,
    terminal_state_proven,
    upsert_status_comment,
)
from apps.github_release_train_spec import (  # noqa: E402
    GoalDecision,
    GoalDisposition,
    GoalPhase,
    GoalPhaseContext,
    GoalReasonCode,
    STATUS_COMMENT_MARKER,
    phase_goal_decision,
    select_ui_runtime,
)


EXIT_BLOCKED = 2
EXIT_AWAITING_UI = 3
EXIT_RESUMED = 4
EXIT_OWN_ACTION = 5
EXIT_CONTINUE_WAITING = 6
EXIT_TERMINAL_FAILURE = 7
EXIT_CONTINUE_SAFE_PHASES = 8
EXIT_AWAIT_PHASE_CAPABILITY = 9
EXIT_INTERRUPTED = 130


def local_playwright_preflight(
    *,
    own_pr: int,
    launch_probe: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Actually import Playwright and launch one fresh non-persistent Chromium context."""

    attempts: list[dict[str, Any]] = []
    playwright_available = False
    chromium_launchable = False
    isolated_context_created = False
    error = ""
    try:
        if launch_probe is None:
            from playwright.sync_api import sync_playwright

            playwright_available = True
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    try:
                        isolated_context_created = True
                    finally:
                        context.close()
                finally:
                    browser.close()
        else:
            playwright_available = True
            launch_probe()
            isolated_context_created = True
        chromium_launchable = True
        attempts.append(
            {
                "operation": "local-playwright-isolated-chromium-launch",
                "status": "success",
            }
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        attempts.append(
            {
                "operation": "local-playwright-isolated-chromium-launch",
                "status": "failed",
                "error": error,
            }
        )
    runtime = select_ui_runtime(
        execution_surface="codex-cli",
        playwright_available=playwright_available,
        chromium_launchable=chromium_launchable and isolated_context_created,
        repo_owned_recovery_available=not chromium_launchable,
    )
    return {
        "kind": "ui-runtime-preflight",
        "own_pr": own_pr,
        "action_pr": own_pr,
        "execution_surface": "codex-cli",
        "runtime": runtime.runtime.value,
        "embedded_browser_required": False,
        "playwright_available": playwright_available,
        "chromium_launchable": chromium_launchable,
        "isolated_context_created": isolated_context_created,
        "continue_ui_flow": runtime.continue_ui_flow,
        "canonical_reason_code": runtime.reason_code,
        "error": error,
        "attempts": attempts,
        "repo_owned_action_available": not chromium_launchable,
        "remediation_exhausted": False,
        "user_intervention_required": False,
    }


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


def _status_metadata(body: str) -> dict[str, Any] | None:
    prefix = f"<!-- {STATUS_COMMENT_MARKER} "
    for line in body.splitlines():
        if not (line.startswith(prefix) and line.endswith(" -->")):
            continue
        try:
            payload = json.loads(line[len(prefix) : -4])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _canonical_pull_state(pull: Mapping[str, Any], labels: set[str]) -> dict[str, Any]:
    return {
        "pr": int(pull.get("number") or 0),
        "github_pr_state": str(pull.get("state") or ""),
        "merged": bool(pull.get("merged")),
        "head_sha": str((pull.get("head") or {}).get("sha") or "").lower(),
        "merge_sha": str(pull.get("merge_commit_sha") or "").lower(),
        "release_state": release_state_from_labels(labels),
        "needs_resume": NEEDS_RESUME_LABEL in labels,
        "loop_root": loop_root_from_labels(labels) or 0,
        "labels": sorted(labels),
    }


def _proven_terminal_loop_member(api: GitHubApi, root: int) -> dict[str, Any] | None:
    if root <= 0:
        return None
    for item in api.list_issues_by_label(loop_root_label(root), state="all"):
        if "pull_request" not in item:
            continue
        pull = api.get_pull(int(item.get("number") or 0))
        labels = label_names(pull)
        if PRODUCTION_LABEL in labels and terminal_state_proven(api, pull):
            return pull
    return None


def _lost_owner_evidence(
    api: GitHubApi,
    pull: Mapping[str, Any],
    labels: set[str],
) -> dict[str, Any] | None:
    """Require the overlay plus the exact machine status written by lost-owner detection."""

    if NEEDS_RESUME_LABEL not in labels:
        return None
    expected = _canonical_pull_state(pull, labels)
    matching: list[dict[str, Any]] = []
    for item in api.list_comments(int(pull.get("number") or 0)):
        metadata = _status_metadata(str(item.get("body") or ""))
        if not metadata:
            continue
        try:
            heartbeat = float(metadata.get("heartbeat"))
        except (TypeError, ValueError):
            continue
        if (
            int(metadata.get("pr") or 0) == expected["pr"]
            and str(metadata.get("head") or "").lower() == expected["head_sha"]
            and int(metadata.get("root") or 0) == expected["loop_root"]
            and str(metadata.get("state") or "") == expected["release_state"]
        ):
            matching.append({**metadata, "heartbeat": heartbeat})
    if not matching:
        return None
    latest = max(matching, key=lambda item: float(item["heartbeat"]))
    if str(latest.get("owner") or "") != "unowned":
        return None
    return {
        "kind": "lost-owner",
        "release_needs_resume": True,
        "owner": "unowned",
        "head_sha": expected["head_sha"],
        "loop_root": expected["loop_root"],
        "release_state": expected["release_state"],
        "heartbeat": latest.get("heartbeat"),
        "repo_owned_action_available": True,
    }


def _decision(
    disposition: GoalDisposition,
    *,
    own_pr: int,
    action_pr: int,
    state: Mapping[str, Any],
    reason: GoalReasonCode | str,
    next_action: str,
    evidence: tuple[Mapping[str, Any], ...],
) -> GoalDecision:
    reason_code = reason.value if isinstance(reason, GoalReasonCode) else str(reason)
    return GoalDecision(
        disposition=disposition,
        own_pr=own_pr,
        action_pr=action_pr,
        canonical_github_state=state,
        reason_code=reason_code,
        allowed_next_action=next_action,
        user_intervention_required=False,
        evidence=evidence,
        remediation_exhausted=False,
        current_phase=GoalPhase.GITHUB_RELEASE,
        blocked_phase=None,
        safe_phases_remaining=(),
        required_capability="",
        capability_evidence=(),
        next_executable_action=next_action,
    )


def _evidence_blocker_decision(
    *,
    own_pr: int,
    action_pr: int,
    state: Mapping[str, Any],
    blocker_evidence: Mapping[str, Any] | None,
) -> GoalDecision | None:
    """Accept blocked only from exact, exhausted evidence with no repo-owned action."""

    if not blocker_evidence:
        return None
    attempts = blocker_evidence.get("attempts")
    canonical_target = state.get("action") if action_pr != own_pr else state.get("own")
    if not isinstance(canonical_target, Mapping):
        raise ValueError("blocker evidence target is absent from canonical GitHub state")
    if (
        int(blocker_evidence.get("own_pr") or 0) != own_pr
        or int(blocker_evidence.get("action_pr") or 0) != action_pr
        or str(blocker_evidence.get("head_sha") or "").lower()
        != str(canonical_target.get("head_sha") or "").lower()
        or str(blocker_evidence.get("release_state") or "")
        != str(canonical_target.get("release_state") or "")
        or int(blocker_evidence.get("loop_root") or 0)
        != int(canonical_target.get("loop_root") or 0)
        or str(blocker_evidence.get("merge_sha") or "").lower()
        != str(canonical_target.get("merge_sha") or "").lower()
        or not isinstance(attempts, list)
        or not attempts
        or blocker_evidence.get("remediation_exhausted") is not True
        or blocker_evidence.get("repo_owned_action_available") is not False
    ):
        raise ValueError(
            "blocker evidence must bind exact own/action PR, head/state/root/merge, list attempts, "
            "exhaust remediation, and prove no repo-owned action remains"
        )
    terminal_failure = blocker_evidence.get("terminal_failure") is True
    user_required = blocker_evidence.get("user_intervention_required") is True
    if terminal_failure == user_required:
        raise ValueError(
            "blocker evidence must prove exactly one of terminal failure or user intervention"
        )
    reason = str(
        blocker_evidence.get("canonical_reason_code")
        or (
            GoalReasonCode.PROTOCOL_IRRECOVERABLE.value
            if terminal_failure
            else GoalReasonCode.EXTERNAL_AUTHORITY_REQUIRED.value
        )
    )
    user_action = str(blocker_evidence.get("minimal_user_action") or "")
    if user_required and not user_action:
        raise ValueError("external blocker evidence requires a minimal human-only action")
    return GoalDecision(
        disposition=(
            GoalDisposition.TERMINAL_FAILURE
            if terminal_failure
            else GoalDisposition.EXTERNAL_BLOCKER
        ),
        own_pr=own_pr,
        action_pr=action_pr,
        canonical_github_state=state,
        reason_code=reason,
        allowed_next_action=user_action,
        user_intervention_required=user_required,
        evidence=(dict(blocker_evidence),),
        remediation_exhausted=True,
        current_phase=GoalPhase.GITHUB_RELEASE,
        blocked_phase=GoalPhase.GITHUB_RELEASE,
        safe_phases_remaining=(),
        required_capability=str(blocker_evidence.get("required_capability") or ""),
        capability_evidence=(dict(blocker_evidence),),
        next_executable_action=user_action,
    )


def goal_disposition(
    api: GitHubApi,
    own_pr: int,
    *,
    blocker_evidence: Mapping[str, Any] | None = None,
    phase_context: GoalPhaseContext | None = None,
) -> GoalDecision:
    """Return the only canonical Goal interpretation for own PR and global gate."""

    own_pull = api.get_pull(own_pr)
    own_labels = label_names(own_pull)
    task_class = task_class_from_labels(own_labels)
    scope = scope_from_labels(own_labels)
    own = _canonical_pull_state(own_pull, own_labels)
    queue = queue_gate_state(api)
    queue_state = {
        "status": str(queue.get("status") or "idle"),
        "gate_pr": int(queue.get("pr_number") or 0),
        "loop_root": int(queue.get("loop_root") or 0),
        "reason": str(queue.get("reason") or ""),
    }
    canonical: dict[str, Any] = {
        "own": own,
        "queue": queue_state,
        "task_class": task_class,
        "scope": scope,
    }
    own_evidence: tuple[Mapping[str, Any], ...] = (
        {"kind": "own-pr", **own},
        {"kind": "queue", **queue_state},
    )

    if phase_context is not None:
        phase_decision = phase_goal_decision(
            phase_context,
            own_pr=own_pr,
            action_pr=own_pr,
            canonical_github_state=canonical,
        )
        if phase_decision is not None:
            return phase_decision

    follows_existing_chain = (
        own["loop_root"] == own_pr
        or own["merged"]
        or own["release_state"] == SUPERSEDED_LABEL
    )
    if task_class == LOOP_TASK_LABEL and own["loop_root"] and follows_existing_chain:
        terminal_member = _proven_terminal_loop_member(api, int(own["loop_root"]))
        if terminal_member is not None:
            terminal_labels = label_names(terminal_member)
            terminal = _canonical_pull_state(terminal_member, terminal_labels)
            canonical["action"] = terminal
            return _decision(
                GoalDisposition.TERMINAL_SUCCESS,
                own_pr=own_pr,
                action_pr=int(terminal_member.get("number") or 0),
                state=canonical,
                reason=GoalReasonCode.TERMINAL_PROOF_VERIFIED,
                next_action="",
                evidence=(*own_evidence, {"kind": "terminal-chain-member", **terminal}),
            )

    expected_terminal = (
        PRODUCTION_LABEL
        if task_class == LOOP_TASK_LABEL or scope != REPO_ONLY_LABEL
        else DONE_LABEL
    )
    if own["release_state"] == expected_terminal:
        if terminal_state_proven(api, own_pull):
            return _decision(
                GoalDisposition.TERMINAL_SUCCESS,
                own_pr=own_pr,
                action_pr=own_pr,
                state=canonical,
                reason=GoalReasonCode.TERMINAL_PROOF_VERIFIED,
                next_action="",
                evidence=own_evidence,
            )
        evidence_decision = _evidence_blocker_decision(
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            blocker_evidence=blocker_evidence,
        )
        if evidence_decision is not None:
            return evidence_decision
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.TERMINAL_PROOF_MISSING,
            next_action="run repo-owned exact-SHA terminal reconciliation",
            evidence=own_evidence,
        )

    evidence_decision = None
    blocker_candidate_states = {AWAITING_UI_LABEL, BLOCKED_LABEL, HALTED_LABEL}
    if (
        own["release_state"] in blocker_candidate_states
        and (not blocker_evidence or int(blocker_evidence.get("action_pr") or 0) == own_pr)
    ):
        evidence_decision = _evidence_blocker_decision(
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            blocker_evidence=blocker_evidence,
        )
    if evidence_decision is not None:
        return evidence_decision

    if NEEDS_RESUME_LABEL in own_labels:
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.OWN_RELEASE_RESUME_REQUIRED,
            next_action=(
                f"python3 apps/github_release_train_wait.py {own_pr} "
                "--resume-owner --no-ack-agent"
            ),
            evidence=own_evidence,
        )
    if own["release_state"] == AWAITING_UI_LABEL:
        return _decision(
            GoalDisposition.RECOVER_OWN_CHAIN,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.OWN_UI_FLOW_REQUIRED,
            next_action=(
                "run the repo-owned production UI Flow with local isolated Playwright/Chromium; "
                "accept exact deployed SHA only on sufficient evidence, otherwise keep fail-closed "
                "or create same-root recovery"
            ),
            evidence=own_evidence,
        )
    if own["release_state"] == AWAITING_AGENT_LABEL:
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.OWN_AGENT_ACK_REQUIRED,
            next_action=f"python3 apps/github_release_train_wait.py {own_pr}",
            evidence=own_evidence,
        )
    if own["release_state"] in {BLOCKED_LABEL, HALTED_LABEL}:
        reason = (
            GoalReasonCode.HALTED_RECONCILIATION_AVAILABLE
            if own["release_state"] == HALTED_LABEL
            else GoalReasonCode.OWN_RELEASE_REMEDIATION_AVAILABLE
        )
        action = (
            "run repo-owned exact-SHA halted reconciliation and re-evaluate"
            if own["release_state"] == HALTED_LABEL
            else "inspect the canonical failure evidence, apply the bounded fix, run baseline, "
            "then use the trusted exact-head retry/correction path"
        )
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=reason,
            next_action=action,
            evidence=own_evidence,
        )

    queue_pr = int(queue.get("pr_number") or 0)
    queue_status = str(queue.get("status") or "idle")
    if queue_status == "gate-conflict":
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.QUEUE_RECONCILIATION_AVAILABLE,
            next_action="run the repo-owned queue reconciliation and re-evaluate exact gate state",
            evidence=own_evidence,
        )
    if queue_pr > 0 and queue_pr != own_pr:
        action_pull = api.get_pull(queue_pr)
        action_labels = label_names(action_pull)
        action = _canonical_pull_state(action_pull, action_labels)
        canonical["action"] = action
        foreign_evidence: tuple[Mapping[str, Any], ...] = (
            *own_evidence,
            {"kind": "action-pr", **action},
        )
        if queue_status == "halted":
            foreign_blocker = _evidence_blocker_decision(
                own_pr=own_pr,
                action_pr=queue_pr,
                state=canonical,
                blocker_evidence=blocker_evidence,
            )
            if foreign_blocker is not None:
                return foreign_blocker
            return _decision(
                GoalDisposition.OWN_ACTION,
                own_pr=own_pr,
                action_pr=queue_pr,
                state=canonical,
                reason=GoalReasonCode.HALTED_RECONCILIATION_AVAILABLE,
                next_action="run the repo-owned exact-SHA halted reconciliation for the gate owner",
                evidence=foreign_evidence,
            )
        if NEEDS_RESUME_LABEL in action_labels:
            lost_owner = _lost_owner_evidence(api, action_pull, action_labels)
            try:
                registration = loop_registration_kind(api, action_pull)
            except (ReleaseBlocked, ReleaseClassificationBlocked) as exc:
                registration = f"invalid:{exc}"
            identity = {
                "kind": "loop-identity",
                "registration": registration,
                "exact_head_verified": (
                    not registration.startswith("invalid:") and len(str(action["head_sha"])) == 40
                ),
                "exact_deployed_sha": action["merge_sha"],
                "exact_deployed_sha_verified": (
                    queue_status != "awaiting-ui" or len(str(action["merge_sha"])) == 40
                ),
                "own_root": own["loop_root"],
                "action_root": action["loop_root"],
                "root_relationship": (
                    "same-root" if own["loop_root"] == action["loop_root"] else "independent-root"
                ),
                "root_isolation_preserved": True,
                "repo_owned_action_available": True,
            }
            if (
                lost_owner is not None
                and identity["exact_head_verified"]
                and identity["exact_deployed_sha_verified"]
            ):
                return _decision(
                    GoalDisposition.TAKEOVER_PREDECESSOR,
                    own_pr=own_pr,
                    action_pr=queue_pr,
                    state=canonical,
                    reason=GoalReasonCode.FOREIGN_GATE_NEEDS_TAKEOVER,
                    next_action=(
                        f"python3 apps/github_release_train_wait.py {queue_pr} "
                        "--resume-owner --no-ack-agent"
                    ),
                    evidence=(*foreign_evidence, lost_owner, identity),
                )
            return _decision(
                GoalDisposition.OWN_ACTION,
                own_pr=own_pr,
                action_pr=queue_pr,
                state=canonical,
                reason=GoalReasonCode.LOST_OWNER_EVIDENCE_INCOMPLETE,
                next_action="run repo-owned gate reconciliation; takeover is forbidden until exact owner/root evidence is proven",
                evidence=(*foreign_evidence, identity),
            )
        if task_class == LOOP_TASK_LABEL and own["loop_root"] == action["loop_root"]:
            return _decision(
                (
                    GoalDisposition.RECOVER_OWN_CHAIN
                    if queue_status == "awaiting-ui"
                    else GoalDisposition.OWN_ACTION
                ),
                own_pr=own_pr,
                action_pr=queue_pr,
                state=canonical,
                reason=(
                    GoalReasonCode.OWN_UI_FLOW_REQUIRED
                    if queue_status == "awaiting-ui"
                    else GoalReasonCode.OWN_AGENT_ACK_REQUIRED
                    if queue_status == "awaiting-agent"
                    else GoalReasonCode.OWN_RELEASE_REMEDIATION_AVAILABLE
                ),
                next_action=(
                    "run the repo-owned production UI Flow with local isolated Playwright/Chromium; "
                    "accept exact deployed SHA only on sufficient evidence, otherwise keep fail-closed "
                    "or create same-root recovery"
                    if queue_status == "awaiting-ui"
                    else f"python3 apps/github_release_train_wait.py {queue_pr}"
                ),
                evidence=foreign_evidence,
            )
        return _decision(
            GoalDisposition.CONTINUE_WAITING,
            own_pr=own_pr,
            action_pr=queue_pr,
            state=canonical,
            reason=GoalReasonCode.FOREIGN_OWNER_ACTIVE,
            next_action=f"python3 apps/github_release_train_wait.py {own_pr} --shepherd",
            evidence=foreign_evidence,
        )

    if own["release_state"] == "release:none":
        return _decision(
            GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=own_pr,
            state=canonical,
            reason=GoalReasonCode.OWN_RELEASE_ENQUEUE_REQUIRED,
            next_action="finish pre-release proof and enqueue the PR through its canonical task-class path",
            evidence=own_evidence,
        )
    return _decision(
        GoalDisposition.CONTINUE_WAITING,
        own_pr=own_pr,
        action_pr=own_pr,
        state=canonical,
        reason=GoalReasonCode.NORMAL_QUEUE_WAITING,
        next_action=f"python3 apps/github_release_train_wait.py {own_pr} --shepherd",
        evidence=own_evidence,
    )


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


def shepherd_release(
    api: GitHubApi,
    own_pr: int,
    *,
    status_seconds: float,
    poll_seconds: float,
    once: bool,
    blocker_evidence: Mapping[str, Any] | None = None,
    phase_context: GoalPhaseContext | None = None,
    owner: str = "codex-cli",
    emit: Callable[[str], None] = print,
) -> int:
    """Observe the whole queue and emit canonical machine-readable Goal decisions."""

    next_status = time.monotonic() + status_seconds if status_seconds > 0 else None
    previous = ""
    while True:
        decision = goal_disposition(
            api,
            own_pr,
            blocker_evidence=blocker_evidence,
            phase_context=phase_context,
        )
        rendered = json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True)
        if rendered != previous:
            emit(rendered)
            previous = rendered
        disposition = decision.disposition
        if disposition == GoalDisposition.TERMINAL_SUCCESS:
            return 0
        if disposition == GoalDisposition.EXTERNAL_BLOCKER:
            return EXIT_BLOCKED
        if disposition == GoalDisposition.TERMINAL_FAILURE:
            return EXIT_TERMINAL_FAILURE
        if disposition == GoalDisposition.CONTINUE_SAFE_PHASES:
            return EXIT_CONTINUE_SAFE_PHASES
        if disposition == GoalDisposition.AWAIT_PHASE_CAPABILITY:
            return EXIT_AWAIT_PHASE_CAPABILITY
        if disposition == GoalDisposition.RECOVER_OWN_CHAIN:
            return EXIT_AWAITING_UI
        if disposition == GoalDisposition.TAKEOVER_PREDECESSOR:
            return EXIT_RESUMED
        if disposition == GoalDisposition.OWN_ACTION:
            return EXIT_OWN_ACTION
        if disposition != GoalDisposition.CONTINUE_WAITING:
            raise RuntimeError(f"unsupported Goal disposition: {disposition.value}")
        if once:
            return EXIT_CONTINUE_WAITING
        now = time.monotonic()
        if next_status is not None and now >= next_status:
            upsert_status_comment(
                api,
                own_pr,
                owner=owner,
                reason=(
                    f"Goal disposition {disposition.value}: {decision.reason_code}; "
                    "unchanged queue state is normal waiting"
                ),
                last_action="Codex CLI shepherd heartbeat",
                intervention=False,
            )
            next_status = now + status_seconds
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
    parser = argparse.ArgumentParser(
        description="Wait for or shepherd a wb-core Release Train PR",
        epilog=(
            "Shepherd exits: 0 terminal success; 2 proven EXTERNAL_BLOCKER; "
            "3 own LOOP UI/recovery action; 4 predecessor takeover/resume next action; "
            "5 other own repo action; 6 --once normal waiting; 7 proven TERMINAL_FAILURE; "
            "8 CONTINUE_SAFE_PHASES; 9 AWAIT_PHASE_CAPABILITY; "
            "130 interrupt. Elapsed time is never terminal."
        ),
    )
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
    parser.add_argument(
        "--shepherd",
        action="store_true",
        help="emit canonical machine-readable Goal disposition for own PR and global gate",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="with --shepherd, return code 6 after one CONTINUE_WAITING observation",
    )
    parser.add_argument(
        "--blocker-evidence",
        type=Path,
        help=(
            "JSON evidence for a proposed EXTERNAL_BLOCKER/TERMINAL_FAILURE; rejected unless "
            "exact PR/head/state/root/merge, attempts, exhausted remediation and absence of "
            "repo-owned action are proven"
        ),
    )
    parser.add_argument(
        "--phase-state",
        type=Path,
        help=(
            "JSON keys: current_phase, safe_phases_remaining, required_capability, "
            "capability_available, capability_evidence, repo_owned_remediation_available, "
            "remediation_exhausted, user_intervention_required, next_executable_action, "
            "minimal_user_action; future production capability cannot block safe phases"
        ),
    )
    parser.add_argument(
        "--playwright-preflight",
        action="store_true",
        help=(
            "actually import local Playwright and launch an isolated Chromium context; "
            "embedded Browser availability is intentionally ignored"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.once and not args.shepherd:
            raise ValueError("--once requires --shepherd")
        if args.blocker_evidence is not None and not args.shepherd:
            raise ValueError("--blocker-evidence requires --shepherd")
        if args.phase_state is not None and not args.shepherd:
            raise ValueError("--phase-state requires --shepherd")
        if args.playwright_preflight:
            report = local_playwright_preflight(own_pr=args.pr)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["continue_ui_flow"] else EXIT_OWN_ACTION
        blocker_evidence: Mapping[str, Any] | None = None
        if args.blocker_evidence is not None:
            loaded = json.loads(args.blocker_evidence.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("blocker evidence must be a JSON object")
            blocker_evidence = loaded
        phase_context: GoalPhaseContext | None = None
        if args.phase_state is not None:
            loaded_phase = json.loads(args.phase_state.read_text(encoding="utf-8"))
            if not isinstance(loaded_phase, dict):
                raise ValueError("phase state must be a JSON object")
            phase_context = GoalPhaseContext.from_mapping(loaded_phase)
        ensure_ca_bundle()
        api = GitHubApi(
            repository=_repository(args.repository),
            token=_token(),
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        if args.shepherd:
            return shepherd_release(
                api,
                args.pr,
                status_seconds=args.status_seconds,
                poll_seconds=args.poll_seconds,
                once=args.once,
                blocker_evidence=blocker_evidence,
                phase_context=phase_context,
                owner=args.owner,
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
