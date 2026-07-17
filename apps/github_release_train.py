"""GitHub-native serialized merge/deploy queue for wb-core.

The queue keeps all durable state on pull requests through repository labels.
It never discovers work implicitly: only an open pull request carrying the
``release:ready`` label is eligible.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


READY_LABEL = "release:ready"
RUNNING_LABEL = "release:running"
AWAITING_AGENT_LABEL = "release:awaiting-agent"
AWAITING_UI_LABEL = "release:awaiting-ui"
BLOCKED_LABEL = "release:blocked"
DONE_LABEL = "release:done"
PRODUCTION_LABEL = "release:production"
HALTED_LABEL = "release:halted"

REPO_ONLY_LABEL = "scope:repo-only"
LIVE_RUNTIME_LABEL = "scope:live-runtime"
PRODUCTION_MUTATION_LABEL = "scope:production-mutation"

STANDARD_TASK_LABEL = "task:standard"
LOOP_TASK_LABEL = "task:loop"
LOOP_ROOT_PREFIX = "loop:root-"
LOOP_ACK_PREFIX = "loop:ack-"
LOOP_ACCEPT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

STATE_LABELS = {
    READY_LABEL,
    RUNNING_LABEL,
    AWAITING_AGENT_LABEL,
    AWAITING_UI_LABEL,
    BLOCKED_LABEL,
    DONE_LABEL,
    PRODUCTION_LABEL,
    HALTED_LABEL,
}
SCOPE_LABELS = {
    REPO_ONLY_LABEL,
    LIVE_RUNTIME_LABEL,
    PRODUCTION_MUTATION_LABEL,
}
TASK_LABELS = {
    STANDARD_TASK_LABEL,
    LOOP_TASK_LABEL,
}
LABEL_DEFINITIONS = {
    READY_LABEL: ("0E8A16", "Проверки завершены; PR явно поставлен в очередь выпуска"),
    RUNNING_LABEL: ("FBCA04", "Release Train обрабатывает PR"),
    AWAITING_AGENT_LABEL: ("D4C5F9", "LOOP ждёт acknowledgement активной Codex-сессии"),
    AWAITING_UI_LABEL: ("A371F7", "LOOP задеплоен и ждёт production UI acceptance"),
    BLOCKED_LABEL: ("D93F0B", "PR остановлен до исправления конкретного blocker"),
    DONE_LABEL: ("5319E7", "Repo-only PR смёржен; deploy не применяется"),
    PRODUCTION_LABEL: ("1D76DB", "Merge SHA задеплоен и проверен в production"),
    HALTED_LABEL: ("B60205", "Production verify не прошёл; вся очередь остановлена"),
    REPO_ONLY_LABEL: ("C5DEF5", "Execution-контур repo-only без deploy"),
    LIVE_RUNTIME_LABEL: ("BFDADC", "Execution-контур live/runtime с deploy и verify"),
    PRODUCTION_MUTATION_LABEL: ("E99695", "Production data mutation: обязательный human gate"),
    STANDARD_TASK_LABEL: ("D1D5DA", "Обычная задача с полным применимым closure"),
    LOOP_TASK_LABEL: ("8B5CF6", "Итерационная задача с production UI Flow"),
}

REQUIRED_DEPLOY_ENV = (
    "WB_CORE_DEPLOY_SSH_KEY",
    "WB_CORE_DEPLOY_KNOWN_HOSTS",
)
TERMINAL_CHECK_FAILURES = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}


class ReleaseTrainError(RuntimeError):
    """Base operator-visible release-train failure."""


class ReleaseBlocked(ReleaseTrainError):
    """A bounded PR-specific blocker that must not halt unrelated queued PRs."""


class GitHubApiError(ReleaseTrainError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.payload = payload


class ReleaseApi(Protocol):
    def ensure_label(self, name: str, color: str, description: str) -> None: ...

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]: ...

    def get_pull(self, number: int) -> dict[str, Any]: ...

    def compare(self, base: str, head: str) -> dict[str, Any]: ...

    def update_branch(self, number: int, expected_head_sha: str) -> None: ...

    def dispatch_workflow(self, workflow: str, ref: str) -> None: ...

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]: ...

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]: ...

    def add_labels(self, number: int, labels: Iterable[str]) -> None: ...

    def remove_label(self, number: int, label: str) -> None: ...

    def add_comment(self, number: int, body: str) -> None: ...

    def delete_branch(self, branch: str) -> None: ...


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    head_sha: str
    head_ref: str
    scope: str
    task_class: str
    loop_root: int | None = None
    agent_acknowledged: bool = False

    @property
    def deploy_required(self) -> bool:
        return self.scope == LIVE_RUNTIME_LABEL


@dataclass(frozen=True)
class MergeResult:
    merge_sha: str
    skip_release: bool = False


class GitHubApi:
    def __init__(self, *, repository: str, token: str, api_url: str) -> None:
        if "/" not in repository:
            raise ValueError("repository must use owner/name form")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        allowed_statuses: Iterable[int] = (),
    ) -> tuple[Any, Mapping[str, str]]:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "wb-core-release-train",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=30) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else None
                return parsed, dict(response.headers.items())
        except urllib_error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if exc.code in set(allowed_statuses):
                return parsed, dict(exc.headers.items())
            message = "request failed"
            if isinstance(parsed, dict) and parsed.get("message"):
                message = str(parsed["message"])
            raise GitHubApiError(exc.code, message, parsed) from exc

    def _repo_path(self, suffix: str) -> str:
        owner, name = self.repository.split("/", 1)
        return f"/repos/{urllib_parse.quote(owner)}/{urllib_parse.quote(name)}/{suffix.lstrip('/')}"

    def ensure_label(self, name: str, color: str, description: str) -> None:
        encoded = urllib_parse.quote(name, safe="")
        existing, _ = self._request(
            "GET",
            self._repo_path(f"labels/{encoded}"),
            allowed_statuses=(404,),
        )
        payload = {"name": name, "color": color, "description": description}
        if isinstance(existing, dict) and existing.get("name"):
            self._request("PATCH", self._repo_path(f"labels/{encoded}"), payload)
            return
        self._request("POST", self._repo_path("labels"), payload)

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 11):
            query = urllib_parse.urlencode(
                {
                    "labels": label,
                    "state": state,
                    "sort": "created",
                    "direction": "asc",
                    "per_page": 100,
                    "page": page,
                }
            )
            payload, _ = self._request("GET", self._repo_path(f"issues?{query}"))
            batch = list(payload or [])
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return items

    def get_pull(self, number: int) -> dict[str, Any]:
        payload, _ = self._request("GET", self._repo_path(f"pulls/{number}"))
        if not isinstance(payload, dict):
            raise ReleaseTrainError(f"PR #{number} returned an invalid payload")
        return payload

    def compare(self, base: str, head: str) -> dict[str, Any]:
        encoded_base = urllib_parse.quote(base, safe="")
        encoded_head = urllib_parse.quote(head, safe="")
        payload, _ = self._request(
            "GET",
            self._repo_path(f"compare/{encoded_base}...{encoded_head}"),
        )
        if not isinstance(payload, dict):
            raise ReleaseTrainError("compare returned an invalid payload")
        return payload

    def update_branch(self, number: int, expected_head_sha: str) -> None:
        self._request(
            "PUT",
            self._repo_path(f"pulls/{number}/update-branch"),
            {"expected_head_sha": expected_head_sha},
        )

    def dispatch_workflow(self, workflow: str, ref: str) -> None:
        encoded = urllib_parse.quote(workflow, safe="")
        self._request(
            "POST",
            self._repo_path(f"actions/workflows/{encoded}/dispatches"),
            {"ref": ref},
        )

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        query = urllib_parse.urlencode({"per_page": 100, "filter": "latest"})
        payload, _ = self._request(
            "GET",
            self._repo_path(f"commits/{urllib_parse.quote(sha, safe='')}/check-runs?{query}"),
        )
        if not isinstance(payload, dict):
            return []
        return [item for item in payload.get("check_runs") or [] if isinstance(item, dict)]

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]:
        payload, _ = self._request(
            "PUT",
            self._repo_path(f"pulls/{number}/merge"),
            {"sha": expected_head_sha, "merge_method": "squash"},
        )
        if not isinstance(payload, dict):
            raise ReleaseTrainError(f"PR #{number} merge returned an invalid payload")
        return payload

    def add_labels(self, number: int, labels: Iterable[str]) -> None:
        values = sorted({str(label) for label in labels if str(label)})
        if values:
            self._request("POST", self._repo_path(f"issues/{number}/labels"), {"labels": values})

    def remove_label(self, number: int, label: str) -> None:
        encoded = urllib_parse.quote(label, safe="")
        self._request(
            "DELETE",
            self._repo_path(f"issues/{number}/labels/{encoded}"),
            allowed_statuses=(404,),
        )

    def add_comment(self, number: int, body: str) -> None:
        if body.strip():
            self._request("POST", self._repo_path(f"issues/{number}/comments"), {"body": body.strip()})

    def delete_branch(self, branch: str) -> None:
        encoded = urllib_parse.quote(branch, safe="")
        self._request(
            "DELETE",
            self._repo_path(f"git/refs/heads/{encoded}"),
            allowed_statuses=(404,),
        )


def label_names(payload: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in payload.get("labels") or []:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, Mapping) and item.get("name"):
            result.add(str(item["name"]))
    return result


def scope_from_labels(labels: Iterable[str]) -> str:
    matches = sorted(set(labels) & SCOPE_LABELS)
    if len(matches) != 1:
        raise ReleaseBlocked(
            "PR must carry exactly one execution-contour label: "
            + ", ".join(sorted(SCOPE_LABELS))
        )
    return matches[0]


def task_class_from_labels(labels: Iterable[str]) -> str:
    values = set(labels)
    unknown = sorted(label for label in values if label.startswith("task:") and label not in TASK_LABELS)
    matches = sorted(values & TASK_LABELS)
    if unknown or len(matches) != 1:
        detail = ""
        if unknown:
            detail = "; unsupported labels: " + ", ".join(unknown)
        raise ReleaseBlocked(
            "PR must carry exactly one task-class label: "
            + ", ".join(sorted(TASK_LABELS))
            + detail
        )
    return matches[0]


def loop_root_label(number: int) -> str:
    if number <= 0:
        raise ValueError("loop root PR number must be positive")
    return f"{LOOP_ROOT_PREFIX}{number}"


def loop_root_from_labels(labels: Iterable[str]) -> int | None:
    matches = sorted({label for label in labels if label.startswith(LOOP_ROOT_PREFIX)})
    if not matches:
        return None
    if len(matches) != 1:
        raise ReleaseBlocked("PR must carry at most one deterministic loop:root-<PR> label")
    suffix = matches[0][len(LOOP_ROOT_PREFIX) :]
    if not suffix.isdigit() or int(suffix) <= 0:
        raise ReleaseBlocked("loop recovery link must use loop:root-<positive PR number>")
    return int(suffix)


def loop_ack_label(head_sha: str) -> str:
    normalized = head_sha.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("LOOP acknowledgement requires an exact 40-character head SHA")
    return f"{LOOP_ACK_PREFIX}{normalized}"


def loop_ack_labels(labels: Iterable[str]) -> set[str]:
    return {label for label in labels if label.startswith(LOOP_ACK_PREFIX)}


def release_state_from_labels(labels: Iterable[str]) -> str:
    values = set(labels)
    if RUNNING_LABEL in values:
        return RUNNING_LABEL
    matches = sorted(values & (STATE_LABELS - {READY_LABEL, RUNNING_LABEL}))
    if len(matches) > 1:
        raise ReleaseBlocked("PR carries conflicting release state labels")
    if matches:
        return matches[0]
    if READY_LABEL in values:
        return READY_LABEL
    return "release:none"


def _active_gate(api: ReleaseApi, label: str) -> dict[str, Any] | None:
    gates = [
        item
        for item in api.list_issues_by_label(label, state="all")
        if "pull_request" in item
    ]
    if len(gates) > 1:
        numbers = ", ".join(f"#{int(item.get('number') or 0)}" for item in gates)
        raise ReleaseTrainError(f"multiple active {label} gates: {numbers}")
    return gates[0] if gates else None


def _validate_task_context(
    api: ReleaseApi,
    number: int,
    labels: Iterable[str],
    scope: str,
) -> tuple[str, int | None]:
    values = set(labels)
    task_class = task_class_from_labels(values)
    root = loop_root_from_labels(values)
    ui_gate = _active_gate(api, AWAITING_UI_LABEL)
    if task_class == STANDARD_TASK_LABEL:
        if root is not None or loop_ack_labels(values):
            raise ReleaseBlocked("STANDARD PR cannot carry LOOP recovery or acknowledgement labels")
        if ui_gate is not None:
            raise ReleaseBlocked("an exclusive LOOP UI gate is active; unrelated PRs must wait")
        return task_class, None
    if scope != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("LOOP PR must use scope:live-runtime")
    if ui_gate is None:
        if root is not None:
            raise ReleaseBlocked("LOOP recovery link is stale because no release:awaiting-ui gate is active")
        return task_class, None
    gate_labels = label_names(ui_gate)
    gate_root = loop_root_from_labels(gate_labels)
    gate_number = int(ui_gate.get("number") or 0)
    if gate_root is None:
        raise ReleaseTrainError(f"active LOOP gate PR #{gate_number} has no deterministic root label")
    if number == gate_number or root != gate_root:
        raise ReleaseBlocked(
            f"only a recovery PR linked by {loop_root_label(gate_root)} may run during the active LOOP gate"
        )
    return task_class, root


def transition_label_set(current: Iterable[str], state: str) -> set[str]:
    if state not in STATE_LABELS:
        raise ValueError(f"unknown release state label: {state}")
    labels = set(current)
    if state == RUNNING_LABEL:
        labels -= STATE_LABELS - {READY_LABEL}
        labels.add(READY_LABEL)
        labels.add(RUNNING_LABEL)
        return labels
    labels -= STATE_LABELS
    labels.add(state)
    return labels


def set_release_state(
    api: ReleaseApi,
    number: int,
    state: str,
    *,
    current_labels: Iterable[str] | None = None,
    comment: str = "",
) -> None:
    if current_labels is None:
        current_labels = label_names(api.get_pull(number))
    before = set(current_labels)
    after = transition_label_set(before, state)
    api.add_labels(number, sorted(after - before))
    for label in sorted((before & STATE_LABELS) - after):
        api.remove_label(number, label)
    if comment:
        api.add_comment(number, comment)


def select_candidate(api: ReleaseApi) -> dict[str, Any]:
    halted = [
        item
        for item in api.list_issues_by_label(HALTED_LABEL, state="all")
        if "pull_request" in item
    ]
    if halted:
        first = min(halted, key=lambda item: (str(item.get("created_at") or ""), int(item.get("number") or 0)))
        return {
            "status": "halted",
            "found": False,
            "halted_pr_number": int(first.get("number") or 0),
        }

    try:
        agent_gate = _active_gate(api, AWAITING_AGENT_LABEL)
        ui_gate = _active_gate(api, AWAITING_UI_LABEL)
    except ReleaseTrainError as exc:
        return {"status": "gate-conflict", "found": False, "reason": str(exc)}
    if agent_gate is not None:
        return {
            "status": "awaiting-agent",
            "found": False,
            "awaiting_agent_pr_number": int(agent_gate.get("number") or 0),
        }

    ready = [
        item
        for item in api.list_issues_by_label(READY_LABEL, state="open")
        if "pull_request" in item and BLOCKED_LABEL not in label_names(item)
    ]
    if ui_gate is not None:
        try:
            active_root = loop_root_from_labels(label_names(ui_gate))
        except ReleaseBlocked as exc:
            return {"status": "gate-conflict", "found": False, "reason": str(exc)}
        if active_root is None:
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": "active release:awaiting-ui PR has no loop root label",
            }
        linked: list[dict[str, Any]] = []
        for item in ready:
            try:
                if loop_root_from_labels(label_names(item)) == active_root:
                    linked.append(item)
            except ReleaseBlocked:
                continue
        ready = linked
        if not ready:
            return {
                "status": "awaiting-ui",
                "found": False,
                "awaiting_ui_pr_number": int(ui_gate.get("number") or 0),
                "loop_root": active_root,
            }
    if not ready:
        return {"status": "empty", "found": False}
    issue = min(ready, key=lambda item: (str(item.get("created_at") or ""), int(item.get("number") or 0)))
    number = int(issue["number"])
    pull = api.get_pull(number)
    labels = label_names(pull)
    try:
        scope = scope_from_labels(labels)
    except ReleaseBlocked:
        scope = ""
    try:
        task_class = task_class_from_labels(labels)
    except ReleaseBlocked:
        task_class = ""
    return {
        "status": "selected",
        "found": True,
        "pr_number": number,
        "title": str(pull.get("title") or issue.get("title") or ""),
        "head_sha": str((pull.get("head") or {}).get("sha") or ""),
        "head_ref": str((pull.get("head") or {}).get("ref") or ""),
        "scope": scope,
        "task_class": task_class,
    }


def _wait_for_branch_update(
    api: ReleaseApi,
    number: int,
    previous_sha: str,
    *,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        pull = api.get_pull(number)
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        if head_sha and head_sha != previous_sha:
            return pull
        comparison = api.compare("main", head_sha or previous_sha)
        if int(comparison.get("behind_by") or 0) == 0:
            return pull
        if time.monotonic() >= deadline:
            raise ReleaseBlocked("timed out while updating the PR branch from current main")
        time.sleep(poll_seconds)


def wait_for_required_check(
    api: ReleaseApi,
    sha: str,
    check_name: str,
    *,
    timeout_seconds: int,
    poll_seconds: float,
    newer_than_id: int = 0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        matches = [
            item
            for item in api.list_check_runs(sha)
            if str(item.get("name") or "") == check_name
            and int(item.get("id") or 0) > newer_than_id
        ]
        if matches:
            check = max(matches, key=lambda item: int(item.get("id") or 0))
            status = str(check.get("status") or "")
            conclusion = str(check.get("conclusion") or "")
            if status == "completed" and conclusion == "success":
                return check
            if status == "completed" and conclusion in TERMINAL_CHECK_FAILURES:
                raise ReleaseBlocked(f"required check {check_name!r} finished with {conclusion!r}")
            if status == "completed" and conclusion not in {"", "success"}:
                raise ReleaseBlocked(f"required check {check_name!r} is not successful: {conclusion!r}")
        if time.monotonic() >= deadline:
            raise ReleaseBlocked(f"required check {check_name!r} did not succeed before timeout")
        time.sleep(poll_seconds)


def prepare_candidate(
    api: ReleaseApi,
    repository: str,
    number: int,
    *,
    check_name: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> Candidate:
    pull = api.get_pull(number)
    labels = label_names(pull)
    if READY_LABEL not in labels or BLOCKED_LABEL in labels or HALTED_LABEL in labels:
        raise ReleaseBlocked("PR is not in an eligible release:ready state")
    if str(pull.get("state") or "") != "open":
        raise ReleaseBlocked("PR is not open")
    if bool(pull.get("draft")):
        raise ReleaseBlocked("draft PR cannot enter the release queue")
    if str((pull.get("base") or {}).get("ref") or "") != "main":
        raise ReleaseBlocked("release queue accepts only PRs targeting main")
    head_repo = str((((pull.get("head") or {}).get("repo") or {}).get("full_name")) or "")
    if head_repo != repository:
        raise ReleaseBlocked("release queue accepts only same-repository branches")

    scope = scope_from_labels(labels)
    task_class, loop_root = _validate_task_context(api, number, labels, scope)
    if scope == PRODUCTION_MUTATION_LABEL:
        raise ReleaseBlocked("production data mutation requires a separate exact human gate")

    head_sha = str((pull.get("head") or {}).get("sha") or "")
    head_ref = str((pull.get("head") or {}).get("ref") or "")
    if not head_sha or not head_ref:
        raise ReleaseBlocked("PR head identity is missing")
    set_release_state(
        api,
        number,
        RUNNING_LABEL,
        current_labels=labels,
        comment="Release Train начал финальную синхронизацию, проверку и выпуск PR.",
    )
    comparison = api.compare("main", head_sha)
    if int(comparison.get("behind_by") or 0) > 0:
        try:
            api.update_branch(number, head_sha)
        except GitHubApiError as exc:
            raise ReleaseBlocked(f"PR branch cannot be updated from main: {exc}") from exc
        pull = _wait_for_branch_update(
            api,
            number,
            head_sha,
            timeout_seconds=min(timeout_seconds, 300),
            poll_seconds=poll_seconds,
        )
        head_sha = str((pull.get("head") or {}).get("sha") or "")

    previous_check_id = max(
        (
            int(item.get("id") or 0)
            for item in api.list_check_runs(head_sha)
            if str(item.get("name") or "") == check_name
        ),
        default=0,
    )
    api.dispatch_workflow("baseline-ci.yml", head_ref)
    wait_for_required_check(
        api,
        head_sha,
        check_name,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        newer_than_id=previous_check_id,
    )

    pull = api.get_pull(number)
    final_head_sha = str((pull.get("head") or {}).get("sha") or "")
    if final_head_sha != head_sha:
        raise ReleaseBlocked("PR head changed while baseline CI was running; run fresh checks")
    final_labels = label_names(pull)
    if READY_LABEL not in final_labels or BLOCKED_LABEL in final_labels or HALTED_LABEL in final_labels:
        raise ReleaseBlocked("PR release state changed while baseline CI was running")
    if scope_from_labels(final_labels) != scope:
        raise ReleaseBlocked("PR execution scope changed while baseline CI was running")
    final_task_class, final_loop_root = _validate_task_context(api, number, final_labels, scope)
    if final_task_class != task_class or final_loop_root != loop_root:
        raise ReleaseBlocked("PR task class or LOOP recovery link changed while baseline CI was running")
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")):
        raise ReleaseBlocked("PR is no longer an open non-draft change")
    if str((pull.get("base") or {}).get("ref") or "") != "main":
        raise ReleaseBlocked("PR base changed while baseline CI was running")
    if pull.get("mergeable") is False:
        raise ReleaseBlocked("GitHub reports that the PR is not mergeable against current main")
    agent_acknowledged = False
    if task_class == LOOP_TASK_LABEL:
        agent_acknowledged = loop_ack_label(head_sha) in final_labels
    return Candidate(
        number=number,
        title=str(pull.get("title") or ""),
        head_sha=head_sha,
        head_ref=str((pull.get("head") or {}).get("ref") or head_ref),
        scope=scope,
        task_class=task_class,
        loop_root=loop_root,
        agent_acknowledged=agent_acknowledged,
    )


def require_deploy_environment(deploy_env: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_DEPLOY_ENV if not str(deploy_env.get(name) or "").strip()]
    if missing:
        raise ReleaseBlocked("missing GitHub production secrets: " + ", ".join(missing))


def merge_candidate(api: ReleaseApi, candidate: Candidate) -> MergeResult:
    pull = api.get_pull(candidate.number)
    labels = label_names(pull)
    if bool(pull.get("merged")) and str(pull.get("merge_commit_sha") or ""):
        return MergeResult(
            merge_sha=str(pull["merge_commit_sha"]),
            skip_release=RUNNING_LABEL not in labels,
        )
    if READY_LABEL not in labels or BLOCKED_LABEL in labels or HALTED_LABEL in labels:
        raise ReleaseBlocked("PR is no longer in an eligible release state")
    if scope_from_labels(labels) != candidate.scope:
        raise ReleaseBlocked("PR execution scope changed after CI")
    task_class, loop_root = _validate_task_context(api, candidate.number, labels, candidate.scope)
    if task_class != candidate.task_class or loop_root != candidate.loop_root:
        raise ReleaseBlocked("PR task class or LOOP recovery link changed after CI")
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")):
        raise ReleaseBlocked("PR is no longer an open non-draft change")
    if str((pull.get("base") or {}).get("ref") or "") != "main":
        raise ReleaseBlocked("PR base changed after CI")
    current_head_ref = str((pull.get("head") or {}).get("ref") or "")
    if current_head_ref != candidate.head_ref:
        raise ReleaseBlocked("PR head branch changed after CI")
    current_head = str((pull.get("head") or {}).get("sha") or "")
    if current_head != candidate.head_sha:
        raise ReleaseBlocked("PR head changed after CI; enqueue it again after fresh checks")
    comparison = api.compare("main", candidate.head_sha)
    if int(comparison.get("behind_by") or 0) > 0:
        raise ReleaseBlocked("main advanced after CI; synchronize the PR and run fresh checks")
    if candidate.task_class == LOOP_TASK_LABEL:
        acknowledgement = loop_ack_label(candidate.head_sha)
        if acknowledgement not in labels:
            raise ReleaseBlocked("LOOP acknowledgement is missing or does not match the exact head SHA")
        api.remove_label(candidate.number, acknowledgement)
    result = api.merge_pull(candidate.number, candidate.head_sha)
    if not bool(result.get("merged")) or not str(result.get("sha") or ""):
        raise ReleaseBlocked(str(result.get("message") or "GitHub did not merge the PR"))
    return MergeResult(merge_sha=str(result["sha"]))


def cleanup_merged_branch(api: ReleaseApi, repository: str, number: int) -> None:
    pull = api.get_pull(number)
    head = pull.get("head") or {}
    head_repo = str(((head.get("repo") or {}).get("full_name")) or "")
    branch = str(head.get("ref") or "")
    if head_repo == repository and branch and branch != "main":
        api.delete_branch(branch)


def request_loop_agent(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
) -> None:
    pull = api.get_pull(number)
    labels = label_names(pull)
    scope = scope_from_labels(labels)
    task_class, _ = _validate_task_context(api, number, labels, scope)
    if task_class != LOOP_TASK_LABEL:
        raise ReleaseBlocked("release:awaiting-agent applies only to LOOP PRs")
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")):
        raise ReleaseBlocked("LOOP PR must remain open while awaiting agent acknowledgement")
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    if head_sha != expected_head_sha:
        raise ReleaseBlocked("LOOP head changed before acknowledgement request")
    already_waiting = AWAITING_AGENT_LABEL in labels
    for stale in sorted(loop_ack_labels(labels)):
        api.remove_label(number, stale)
    set_release_state(
        api,
        number,
        AWAITING_AGENT_LABEL,
        current_labels=labels - loop_ack_labels(labels),
        comment=(
            "LOOP прошёл финальную синхронизацию и baseline и ждёт acknowledgement для "
            f"PR #{number} на exact head `{head_sha}`."
            if not already_waiting
            else ""
        ),
    )


def acknowledge_loop_agent(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    actor: str,
    association: str,
) -> str:
    if association.upper() not in LOOP_ACCEPT_ASSOCIATIONS:
        raise ReleaseBlocked("LOOP acknowledgement requires repository write association")
    pull = api.get_pull(number)
    labels = label_names(pull)
    actual_head_sha = str((pull.get("head") or {}).get("sha") or "")
    if actual_head_sha != expected_head_sha:
        raise ReleaseBlocked("LOOP acknowledgement head SHA is stale")
    acknowledgement = loop_ack_label(actual_head_sha)
    if acknowledgement in labels and READY_LABEL in labels:
        api.dispatch_workflow("release-train.yml", "main")
        return "already-acknowledged"
    if AWAITING_AGENT_LABEL not in labels:
        raise ReleaseBlocked("PR is not the active release:awaiting-agent LOOP")
    scope = scope_from_labels(labels)
    task_class, _ = _validate_task_context(api, number, labels, scope)
    if task_class != LOOP_TASK_LABEL:
        raise ReleaseBlocked("agent acknowledgement applies only to LOOP PRs")
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")):
        raise ReleaseBlocked("LOOP PR is no longer an open non-draft change")
    for stale in sorted(loop_ack_labels(labels) - {acknowledgement}):
        api.remove_label(number, stale)
    api.ensure_label(
        acknowledgement,
        "6F42C1",
        f"One-shot LOOP agent acknowledgement for exact head {actual_head_sha}",
    )
    api.add_labels(number, [acknowledgement])
    set_release_state(
        api,
        number,
        READY_LABEL,
        current_labels=(labels - loop_ack_labels(labels)) | {acknowledgement},
        comment=f"LOOP acknowledgement от @{actor} принят для exact head `{actual_head_sha}`.",
    )
    api.dispatch_workflow("release-train.yml", "main")
    return "acknowledged"


def mark_loop_awaiting_ui(api: ReleaseApi, number: int, merge_sha: str) -> tuple[int, str]:
    pull = api.get_pull(number)
    labels = label_names(pull)
    if not bool(pull.get("merged")):
        raise ReleaseBlocked("LOOP UI gate can be opened only after merge")
    scope = scope_from_labels(labels)
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("release:awaiting-ui requires task:loop + scope:live-runtime")
    current_root = loop_root_from_labels(labels)
    if PRODUCTION_LABEL in labels:
        if current_root is None:
            raise ReleaseTrainError("accepted LOOP PR has no root label")
        return current_root, "already-accepted"
    gates = [
        item
        for item in api.list_issues_by_label(AWAITING_UI_LABEL, state="all")
        if "pull_request" in item
    ]
    gate_roots = {loop_root_from_labels(label_names(item)) for item in gates}
    if None in gate_roots or len(gate_roots) > 1:
        raise ReleaseTrainError("active LOOP UI gates do not share one deterministic root")
    if any(int(item.get("number") or 0) == number for item in gates):
        if current_root is None:
            raise ReleaseTrainError("active LOOP UI gate has no root label")
        for item in gates:
            previous = int(item.get("number") or 0)
            if previous != number:
                api.remove_label(previous, AWAITING_UI_LABEL)
                api.add_comment(previous, f"Duplicate gate healed in favor of LOOP PR #{number}.")
        return current_root, "already-awaiting-ui"
    if gates and current_root is not None and RUNNING_LABEL not in labels:
        root = next(iter(gate_roots))
        if current_root == root:
            return current_root, "superseded-iteration"
    if not gates:
        if current_root is not None:
            raise ReleaseBlocked("recovery LOOP lost its active parent gate before UI handoff")
        root = number
    else:
        root = next(iter(gate_roots))
        if root <= 0 or current_root != root:
            raise ReleaseBlocked("recovery LOOP does not match the active deterministic loop root")
    root_label = loop_root_label(root)
    api.ensure_label(root_label, "C2A5F8", f"Deterministic recovery chain for LOOP PR #{root}")
    api.add_labels(number, [root_label])
    set_release_state(
        api,
        number,
        AWAITING_UI_LABEL,
        current_labels=labels | {root_label},
        comment=(
            f"LOOP merge `{merge_sha}` задеплоен; PR #{number} ждёт production UI Flow и acceptance."
        ),
    )
    for item in gates:
        previous = int(item.get("number") or 0)
        api.remove_label(previous, AWAITING_UI_LABEL)
        api.add_comment(previous, f"LOOP gate перенесён на recovery PR #{number} в цепочке `{root_label}`.")
    return root, "awaiting-ui"


def accept_loop_ui(
    api: ReleaseApi,
    number: int,
    *,
    actor: str,
    association: str,
) -> str:
    if association.upper() not in LOOP_ACCEPT_ASSOCIATIONS:
        raise ReleaseBlocked("LOOP UI acceptance requires repository write association")
    pull = api.get_pull(number)
    labels = label_names(pull)
    root = loop_root_from_labels(labels)
    if root is None or task_class_from_labels(labels) != LOOP_TASK_LABEL:
        raise ReleaseBlocked("PR is not part of a deterministic LOOP chain")
    active_gate = _active_gate(api, AWAITING_UI_LABEL)
    if active_gate is None:
        if PRODUCTION_LABEL not in labels:
            raise ReleaseBlocked("there is no active release:awaiting-ui gate")
        api.dispatch_workflow("release-train.yml", "main")
        return "already-accepted"
    if int(active_gate.get("number") or 0) != number:
        raise ReleaseBlocked("UI acceptance must target the current LOOP iteration")
    chain_label = loop_root_label(root)
    chain = [
        item
        for item in api.list_issues_by_label(chain_label, state="all")
        if "pull_request" in item
    ]
    if not chain:
        raise ReleaseTrainError("active LOOP chain is empty")
    ordered = sorted(
        chain,
        key=lambda item: (int(item.get("number") or 0) == number, int(item.get("number") or 0)),
    )
    for item in ordered:
        chain_number = int(item.get("number") or 0)
        chain_pull = api.get_pull(chain_number)
        chain_labels = label_names(chain_pull)
        if (
            task_class_from_labels(chain_labels) != LOOP_TASK_LABEL
            or scope_from_labels(chain_labels) != LIVE_RUNTIME_LABEL
            or loop_root_from_labels(chain_labels) != root
            or not bool(chain_pull.get("merged"))
        ):
            raise ReleaseTrainError(f"invalid LOOP chain member PR #{chain_number}")
        set_release_state(
            api,
            chain_number,
            PRODUCTION_LABEL,
            current_labels=chain_labels,
            comment=(
                f"Production UI Flow принят @{actor}; LOOP-цепочка `{chain_label}` завершена."
                if chain_number == number
                else ""
            ),
        )
    api.dispatch_workflow("release-train.yml", "main")
    return "accepted"


def handle_loop_comment(
    api: ReleaseApi,
    number: int,
    command: str,
    *,
    actor: str,
    association: str,
) -> str:
    parts = command.strip().split()
    if len(parts) == 6 and parts[:3] == ["/wb-core", "loop", "ack-agent"]:
        try:
            command_number = int(parts[3])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP acknowledgement PR number") from exc
        if command_number != number or parts[4] != "head":
            raise ReleaseBlocked("LOOP acknowledgement must bind the current PR and exact head")
        return acknowledge_loop_agent(
            api,
            number,
            parts[5],
            actor=actor,
            association=association,
        )
    if len(parts) == 4 and parts[:3] == ["/wb-core", "loop", "accept-ui"]:
        try:
            command_number = int(parts[3])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP acceptance PR number") from exc
        if command_number != number:
            raise ReleaseBlocked("LOOP UI acceptance must bind the current PR")
        return accept_loop_ui(api, number, actor=actor, association=association)
    raise ReleaseBlocked("unsupported LOOP command")


def write_github_output(path: str | None, values: Mapping[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    with output.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value if value is not None else "")
            if "\n" in rendered or "\r" in rendered:
                raise ValueError(f"multiline GitHub output is not allowed for {key}")
            stream.write(f"{key}={rendered}\n")


def _api_from_env() -> GitHubApi:
    return GitHubApi(
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )


def _json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def command_setup(_: argparse.Namespace) -> int:
    api = _api_from_env()
    for name, (color, description) in LABEL_DEFINITIONS.items():
        api.ensure_label(name, color, description)
    _json_print({"status": "ready", "label_count": len(LABEL_DEFINITIONS)})
    return 0


def command_select(args: argparse.Namespace) -> int:
    api = _api_from_env()
    result = select_candidate(api)
    write_github_output(
        args.output_path,
        {
            "status": result.get("status", ""),
            "found": bool(result.get("found")),
            "halted": result.get("status") == "halted",
            "gate_conflict": result.get("status") == "gate-conflict",
            "halted_pr_number": result.get("halted_pr_number", ""),
            "awaiting_agent_pr_number": result.get("awaiting_agent_pr_number", ""),
            "awaiting_ui_pr_number": result.get("awaiting_ui_pr_number", ""),
            "pr_number": result.get("pr_number", ""),
            "head_sha": result.get("head_sha", ""),
            "head_ref": result.get("head_ref", ""),
            "scope": result.get("scope", ""),
            "task_class": result.get("task_class", ""),
        },
    )
    _json_print(result)
    return 0


def command_transition(args: argparse.Namespace) -> int:
    api = _api_from_env()
    set_release_state(api, args.pr, args.state, comment=args.comment or "")
    _json_print({"status": args.state, "pr_number": args.pr})
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    api = _api_from_env()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        candidate = prepare_candidate(
            api,
            repository,
            args.pr,
            check_name=args.check_name,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except ReleaseBlocked as exc:
        set_release_state(
            api,
            args.pr,
            BLOCKED_LABEL,
            comment=f"Release Train остановил PR до merge: `{exc}`",
        )
        _json_print({"status": "blocked", "pr_number": args.pr, "reason": str(exc)})
        return 2
    write_github_output(
        args.output_path,
        {
            "head_sha": candidate.head_sha,
            "head_ref": candidate.head_ref,
            "scope": candidate.scope,
            "deploy_required": candidate.deploy_required,
            "task_class": candidate.task_class,
            "loop_root": candidate.loop_root or 0,
            "agent_acknowledged": candidate.agent_acknowledged,
        },
    )
    _json_print(
        {
            "status": "prepared",
            "pr_number": candidate.number,
            "head_sha": candidate.head_sha,
            "scope": candidate.scope,
            "task_class": candidate.task_class,
            "agent_acknowledged": candidate.agent_acknowledged,
        }
    )
    return 0


def command_preflight_deploy(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        require_deploy_environment(os.environ)
    except ReleaseBlocked as exc:
        set_release_state(
            api,
            args.pr,
            BLOCKED_LABEL,
            comment=f"Release Train остановил live PR до merge: `{exc}`",
        )
        _json_print({"status": "blocked", "pr_number": args.pr, "reason": str(exc)})
        return 2
    _json_print({"status": "ready", "pr_number": args.pr, "deploy_environment": "configured"})
    return 0


def command_merge(args: argparse.Namespace) -> int:
    api = _api_from_env()
    candidate = Candidate(
        number=args.pr,
        title="",
        head_sha=args.expected_head_sha,
        head_ref=args.head_ref,
        scope=args.scope,
        task_class=args.task_class,
        loop_root=args.loop_root or None,
        agent_acknowledged=args.task_class == LOOP_TASK_LABEL,
    )
    try:
        result = merge_candidate(api, candidate)
    except ReleaseBlocked as exc:
        set_release_state(
            api,
            args.pr,
            BLOCKED_LABEL,
            comment=f"Release Train не смёржил PR: `{exc}`",
        )
        _json_print({"status": "blocked", "pr_number": args.pr, "reason": str(exc)})
        return 2
    write_github_output(
        args.output_path,
        {"merge_sha": result.merge_sha, "skip_release": result.skip_release},
    )
    _json_print(
        {
            "status": "already-released" if result.skip_release else "merged",
            "pr_number": args.pr,
            "merge_sha": result.merge_sha,
        }
    )
    return 0


def command_request_agent(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        request_loop_agent(api, args.pr, args.expected_head_sha)
    except ReleaseBlocked as exc:
        set_release_state(
            api,
            args.pr,
            BLOCKED_LABEL,
            comment=f"Release Train не открыл LOOP handshake: `{exc}`",
        )
        _json_print({"status": "blocked", "pr_number": args.pr, "reason": str(exc)})
        return 2
    _json_print({"status": "awaiting-agent", "pr_number": args.pr, "head_sha": args.expected_head_sha})
    return 0


def command_await_ui(args: argparse.Namespace) -> int:
    api = _api_from_env()
    root, status = mark_loop_awaiting_ui(api, args.pr, args.merge_sha)
    _json_print({"status": status, "pr_number": args.pr, "loop_root": root})
    return 0


def command_handle_comment(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        status = handle_loop_comment(
            api,
            args.pr,
            args.command,
            actor=args.actor,
            association=args.association,
        )
    except ReleaseBlocked as exc:
        _json_print({"status": "rejected", "pr_number": args.pr, "reason": str(exc)})
        return 2
    _json_print({"status": status, "pr_number": args.pr})
    return 0


def command_cleanup_branch(args: argparse.Namespace) -> int:
    api = _api_from_env()
    cleanup_merged_branch(api, os.environ.get("GITHUB_REPOSITORY", ""), args.pr)
    _json_print({"status": "cleaned", "pr_number": args.pr})
    return 0


def command_dispatch_next(_: argparse.Namespace) -> int:
    api = _api_from_env()
    api.dispatch_workflow("release-train.yml", "main")
    _json_print({"status": "dispatched", "workflow": "release-train.yml", "ref": "main"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub-native wb-core release train")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup-labels")
    setup.set_defaults(handler=command_setup)

    select = subparsers.add_parser("select")
    select.add_argument("--output-path", default=os.environ.get("GITHUB_OUTPUT"))
    select.set_defaults(handler=command_select)

    transition = subparsers.add_parser("transition")
    transition.add_argument("--pr", type=int, required=True)
    transition.add_argument("--state", choices=sorted(STATE_LABELS), required=True)
    transition.add_argument("--comment", default="")
    transition.set_defaults(handler=command_transition)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--pr", type=int, required=True)
    prepare.add_argument("--check-name", default="baseline")
    prepare.add_argument("--timeout-seconds", type=int, default=3600)
    prepare.add_argument("--poll-seconds", type=float, default=10.0)
    prepare.add_argument("--output-path", default=os.environ.get("GITHUB_OUTPUT"))
    prepare.set_defaults(handler=command_prepare)

    preflight_deploy = subparsers.add_parser("preflight-deploy")
    preflight_deploy.add_argument("--pr", type=int, required=True)
    preflight_deploy.set_defaults(handler=command_preflight_deploy)

    merge = subparsers.add_parser("merge")
    merge.add_argument("--pr", type=int, required=True)
    merge.add_argument("--expected-head-sha", required=True)
    merge.add_argument("--head-ref", required=True)
    merge.add_argument("--scope", choices=sorted(SCOPE_LABELS), required=True)
    merge.add_argument("--task-class", choices=sorted(TASK_LABELS), required=True)
    merge.add_argument("--loop-root", type=int, default=0)
    merge.add_argument("--output-path", default=os.environ.get("GITHUB_OUTPUT"))
    merge.set_defaults(handler=command_merge)

    request_agent = subparsers.add_parser("request-agent")
    request_agent.add_argument("--pr", type=int, required=True)
    request_agent.add_argument("--expected-head-sha", required=True)
    request_agent.set_defaults(handler=command_request_agent)

    await_ui = subparsers.add_parser("await-ui")
    await_ui.add_argument("--pr", type=int, required=True)
    await_ui.add_argument("--merge-sha", required=True)
    await_ui.set_defaults(handler=command_await_ui)

    handle_comment = subparsers.add_parser("handle-comment")
    handle_comment.add_argument("--pr", type=int, required=True)
    handle_comment.add_argument("--command", required=True)
    handle_comment.add_argument("--actor", required=True)
    handle_comment.add_argument("--association", required=True)
    handle_comment.set_defaults(handler=command_handle_comment)

    cleanup = subparsers.add_parser("cleanup-branch")
    cleanup.add_argument("--pr", type=int, required=True)
    cleanup.set_defaults(handler=command_cleanup_branch)

    dispatch_next = subparsers.add_parser("dispatch-next")
    dispatch_next.set_defaults(handler=command_dispatch_next)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
