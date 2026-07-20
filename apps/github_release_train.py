"""GitHub-native serialized merge/deploy queue for wb-core.

The queue keeps all durable state on pull requests through repository labels.
It never discovers work implicitly: only an open pull request carrying the
``release:ready`` label is eligible.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any, Iterable, Mapping, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_train_spec import (
    ACK_PROOF_MARKER,
    AWAITING_AGENT_LABEL,
    AWAITING_UI_LABEL,
    BLOCKED_LABEL,
    CHAIN_AUDIT_MARKER,
    CANONICAL_PRODUCTION_TARGET_ID,
    CLASSIFICATION_BLOCKER_MARKER,
    COMPLETION_PROOF_MARKER,
    DEPLOY_PROOF_MARKER,
    DONE_LABEL,
    HALTED_LABEL,
    HALT_PROOF_MARKER,
    IDENTITY_CORRECTION_PROOF_MARKER,
    NEEDS_RESUME_LABEL,
    NEW_ROOT_PROOF_MARKER,
    PRIMARY_STATE_LABELS,
    PRODUCTION_LABEL,
    READY_LABEL,
    RECONCILE_PROOF_MARKER,
    RECOVERY_PROOF_MARKER,
    RETRY_PROOF_MARKER,
    RUNNING_LABEL,
    STATUS_COMMENT_MARKER,
    SUPERSEDED_LABEL,
    TERMINAL_LABELS,
    assert_state_invariants,
    transition_allowed,
)

REPO_ONLY_LABEL = "scope:repo-only"
LIVE_RUNTIME_LABEL = "scope:live-runtime"
PRODUCTION_MUTATION_LABEL = "scope:production-mutation"

STANDARD_TASK_LABEL = "task:standard"
LOOP_TASK_LABEL = "task:loop"
LOOP_ROOT_PREFIX = "loop:root-"
LOOP_ACK_PREFIX = "loop:ack-"
LOOP_ACCEPT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
DEFAULT_NEEDS_RESUME_AFTER_SECONDS = 30 * 60

STATE_LABELS = set(PRIMARY_STATE_LABELS)
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
    NEEDS_RESUME_LABEL: ("F9D0C4", "LOOP acknowledgement требует возобновления Codex-сессии"),
    BLOCKED_LABEL: ("D93F0B", "PR остановлен до исправления конкретного blocker"),
    DONE_LABEL: ("5319E7", "Repo-only PR смёржен; deploy не применяется"),
    PRODUCTION_LABEL: ("1D76DB", "Merge SHA задеплоен и проверен в production"),
    HALTED_LABEL: ("B60205", "Production verify не прошёл; вся очередь остановлена"),
    SUPERSEDED_LABEL: ("D4C5F9", "Незамёрженная LOOP-итерация заменена завершённой recovery-chain"),
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


def ensure_ca_bundle() -> None:
    """Use a verified platform CA bundle when framework Python has none configured."""

    if os.environ.get("SSL_CERT_FILE", "").strip() or ssl.get_default_verify_paths().cafile:
        return
    for candidate in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/opt/homebrew/etc/ca-certificates/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
    ):
        if Path(candidate).is_file():
            os.environ["SSL_CERT_FILE"] = candidate
            return


class ReleaseTrainError(RuntimeError):
    """Base operator-visible release-train failure."""


class ReleaseBlocked(ReleaseTrainError):
    """A bounded PR-specific blocker that must not halt unrelated queued PRs."""


class ReleaseClassificationBlocked(ReleaseBlocked):
    """LOOP identity is absent, stale, ambiguous, or unsupported by repo-owned proof."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GitHubApiError(ReleaseTrainError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.payload = payload


class ReleaseApi(Protocol):
    def ensure_label(self, name: str, color: str, description: str) -> None: ...

    def list_issues_by_label(self, label: str, *, state: str) -> list[dict[str, Any]]: ...

    def list_issue_events(self, number: int) -> list[dict[str, Any]]: ...

    def list_comments(self, number: int) -> list[dict[str, Any]]: ...

    def get_pull(self, number: int) -> dict[str, Any]: ...

    def compare(self, base: str, head: str) -> dict[str, Any]: ...

    def update_branch(self, number: int, expected_head_sha: str) -> None: ...

    def dispatch_workflow(self, workflow: str, ref: str) -> None: ...

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]: ...

    def merge_pull(self, number: int, expected_head_sha: str) -> dict[str, Any]: ...

    def add_labels(self, number: int, labels: Iterable[str]) -> None: ...

    def set_labels(self, number: int, labels: Iterable[str]) -> None: ...

    def remove_label(self, number: int, label: str) -> None: ...

    def add_comment(self, number: int, body: str) -> None: ...

    def update_comment(self, comment_id: int, body: str) -> None: ...

    def delete_comment(self, comment_id: int) -> None: ...

    def close_pull(self, number: int) -> None: ...

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

    def list_issue_events(self, number: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 11):
            query = urllib_parse.urlencode({"per_page": 100, "page": page})
            payload, _ = self._request(
                "GET",
                self._repo_path(f"issues/{number}/events?{query}"),
            )
            batch = list(payload or [])
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return items

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, 11):
            query = urllib_parse.urlencode({"per_page": 100, "page": page})
            payload, _ = self._request(
                "GET",
                self._repo_path(f"issues/{number}/comments?{query}"),
            )
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

    def set_labels(self, number: int, labels: Iterable[str]) -> None:
        values = sorted({str(label) for label in labels if str(label)})
        self._request("PUT", self._repo_path(f"issues/{number}/labels"), {"labels": values})

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

    def update_comment(self, comment_id: int, body: str) -> None:
        self._request(
            "PATCH",
            self._repo_path(f"issues/comments/{comment_id}"),
            {"body": body.strip()},
        )

    def delete_comment(self, comment_id: int) -> None:
        self._request(
            "DELETE",
            self._repo_path(f"issues/comments/{comment_id}"),
            allowed_statuses=(404,),
        )

    def close_pull(self, number: int) -> None:
        self._request(
            "PATCH",
            self._repo_path(f"issues/{number}"),
            {"state": "closed", "state_reason": "not_planned"},
        )

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
    try:
        assert_state_invariants(values)
    except ValueError as exc:
        raise ReleaseBlocked(str(exc)) from exc
    primary = values & STATE_LABELS
    if RUNNING_LABEL in primary:
        return RUNNING_LABEL
    if primary:
        return next(iter(primary))
    return "release:none"


def _github_timestamp(value: Any) -> float | None:
    rendered = str(value or "").strip()
    if not rendered:
        return None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_label_timestamp(
    api: ReleaseApi,
    number: int,
    label: str,
    *,
    fallback: Mapping[str, Any],
) -> float | None:
    matches: list[float] = []
    for event in api.list_issue_events(number):
        event_label = event.get("label") or {}
        if (
            str(event.get("event") or "") == "labeled"
            and isinstance(event_label, Mapping)
            and str(event_label.get("name") or "") == label
        ):
            timestamp = _github_timestamp(event.get("created_at"))
            if timestamp is not None:
                matches.append(timestamp)
    if matches:
        return max(matches)
    return _github_timestamp(fallback.get("updated_at") or fallback.get("created_at"))


def _proof_marker(marker: str, **values: object) -> str:
    rendered = " ".join(f"{key}={value}" for key, value in sorted(values.items()))
    return f"<!-- {marker} {rendered} -->"


def _has_comment_proof(api: ReleaseApi, number: int, marker: str, **values: object) -> bool:
    expected = _proof_marker(marker, **values)
    for item in api.list_comments(number):
        if expected not in str(item.get("body") or ""):
            continue
        author = item.get("user")
        if isinstance(author, Mapping):
            login = str(author.get("login") or "")
            if login not in {"github-actions", "github-actions[bot]"}:
                continue
        return True
    return False


def _repo_owned_marker_fields(
    api: ReleaseApi,
    number: int,
    marker: str,
) -> list[dict[str, str]]:
    prefix = f"<!-- {marker} "
    matches: list[dict[str, str]] = []
    for item in api.list_comments(number):
        author = item.get("user")
        if isinstance(author, Mapping) and str(author.get("login") or "") not in {
            "github-actions",
            "github-actions[bot]",
        }:
            continue
        for line in str(item.get("body") or "").splitlines():
            if not line.startswith(prefix) or not line.endswith(" -->"):
                continue
            fields: dict[str, str] = {}
            for token in line[len(prefix) : -4].split():
                key, separator, value = token.partition("=")
                if separator:
                    fields[key] = value
            matches.append(fields)
    return matches


def _classification_blocker_unresolved(api: ReleaseApi, number: int) -> bool:
    """Track classification provenance until a later trusted identity proof resolves it."""

    unresolved = False
    resolution_markers = {
        NEW_ROOT_PROOF_MARKER,
        RECOVERY_PROOF_MARKER,
        IDENTITY_CORRECTION_PROOF_MARKER,
    }
    for item in api.list_comments(number):
        author = item.get("user")
        if isinstance(author, Mapping) and str(author.get("login") or "") not in {
            "github-actions",
            "github-actions[bot]",
        }:
            continue
        for line in str(item.get("body") or "").splitlines():
            if not line.startswith("<!-- ") or not line.endswith(" -->"):
                continue
            marker, _, payload = line[5:-4].partition(" ")
            fields = {
                key: value
                for token in payload.split()
                for key, separator, value in (token.partition("="),)
                if separator
            }
            if fields.get("pr") != str(number):
                continue
            if marker == CLASSIFICATION_BLOCKER_MARKER:
                unresolved = True
            elif marker in resolution_markers:
                unresolved = False
    return unresolved


def _loop_root_labels(labels: Iterable[str]) -> set[str]:
    return {label for label in labels if label.startswith(LOOP_ROOT_PREFIX)}


def _exact_head(pull: Mapping[str, Any], expected_head_sha: str) -> str:
    actual = str((pull.get("head") or {}).get("sha") or "").lower()
    expected = expected_head_sha.strip().lower()
    if actual != expected:
        raise ReleaseClassificationBlocked("loop-head-stale", "LOOP enrollment head SHA is stale")
    try:
        loop_ack_label(actual)
    except ValueError as exc:
        raise ReleaseClassificationBlocked(
            "loop-head-invalid", "LOOP enrollment requires an exact 40-character head SHA"
        ) from exc
    return actual


def _has_successful_check(
    api: ReleaseApi,
    head_sha: str,
    check_name: str,
) -> bool:
    return any(
        str(item.get("name") or "") == check_name
        and str(item.get("status") or "") == "completed"
        and str(item.get("conclusion") or "") == "success"
        for item in api.list_check_runs(head_sha)
    )


def _require_loop_operator(association: str) -> None:
    if association.upper() not in LOOP_ACCEPT_ASSOCIATIONS:
        raise ReleaseBlocked("LOOP enrollment requires repository write association")


def _require_enrollable_loop(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    association: str,
    check_name: str,
) -> tuple[dict[str, Any], set[str], str]:
    _require_loop_operator(association)
    pull = api.get_pull(number)
    labels = label_names(pull)
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")) or bool(pull.get("merged")):
        raise ReleaseClassificationBlocked(
            "loop-pr-not-open", "LOOP enrollment requires an open, unmerged, non-draft PR"
        )
    if str((pull.get("base") or {}).get("ref") or "") != "main":
        raise ReleaseClassificationBlocked("loop-base-invalid", "LOOP enrollment requires base main")
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseClassificationBlocked(
            "loop-class-scope-invalid", "LOOP enrollment requires task:loop + scope:live-runtime"
        )
    if labels & TERMINAL_LABELS:
        raise ReleaseClassificationBlocked(
            "loop-terminal-pr", "terminal release state cannot be enrolled or inherited"
        )
    state = release_state_from_labels(labels)
    if state not in {"release:none", READY_LABEL, BLOCKED_LABEL}:
        raise ReleaseClassificationBlocked(
            "loop-state-not-enrollable", f"LOOP enrollment is not allowed from {state}"
        )
    if state == BLOCKED_LABEL and not _classification_blocker_unresolved(api, number):
        raise ReleaseBlocked(
            "technical release:blocked must be repaired through retry-blocked, not LOOP enrollment"
        )
    head_sha = _exact_head(pull, expected_head_sha)
    if not _has_successful_check(api, head_sha, check_name):
        raise ReleaseBlocked(
            f"LOOP enrollment requires successful {check_name!r} on exact head {head_sha}"
        )
    return pull, labels, head_sha


def _terminal_loop_member(api: ReleaseApi, root: int) -> dict[str, Any] | None:
    for item in api.list_issues_by_label(loop_root_label(root), state="all"):
        if "pull_request" not in item:
            continue
        pull = api.get_pull(int(item.get("number") or 0))
        labels = label_names(pull)
        if labels & TERMINAL_LABELS and terminal_state_proven(api, pull):
            return pull
    return None


def _recovery_proof_gate(
    api: ReleaseApi,
    number: int,
    *,
    head_sha: str,
    root: int,
) -> int | None:
    for fields in _repo_owned_marker_fields(api, number, RECOVERY_PROOF_MARKER):
        try:
            gate = int(fields.get("gate", "0"))
            proof_pr = int(fields.get("pr", "0"))
            proof_root = int(fields.get("root", "0"))
        except ValueError:
            continue
        if proof_pr == number and proof_root == root and fields.get("head") == head_sha:
            return gate
    return None


def _has_prior_new_root_proof(api: ReleaseApi, number: int) -> bool:
    for fields in _repo_owned_marker_fields(api, number, NEW_ROOT_PROOF_MARKER):
        try:
            proof_pr = int(fields.get("pr", "0"))
            proof_root = int(fields.get("root", "0"))
        except ValueError:
            continue
        if proof_pr == number and proof_root == number:
            return True
    return False


def _has_prior_recovery_proof(
    api: ReleaseApi,
    number: int,
    *,
    gate: int,
    root: int,
) -> bool:
    for fields in _repo_owned_marker_fields(api, number, RECOVERY_PROOF_MARKER):
        try:
            proof_gate = int(fields.get("gate", "0"))
            proof_pr = int(fields.get("pr", "0"))
            proof_root = int(fields.get("root", "0"))
        except ValueError:
            continue
        if proof_gate == gate and proof_pr == number and proof_root == root:
            return True
    return False


def _refresh_loop_registration_for_retry(
    api: ReleaseApi,
    pull: Mapping[str, Any],
) -> str:
    """Rebind a proven identity to a fixed exact head without changing classification."""

    number = int(pull.get("number") or 0)
    labels = label_names(pull)
    head_sha = str((pull.get("head") or {}).get("sha") or "").lower()
    root = loop_root_from_labels(labels)
    if root is None:
        raise ReleaseClassificationBlocked(
            "loop-root-missing", "LOOP retry cannot infer a missing registration identity"
        )
    if root > number:
        raise ReleaseClassificationBlocked(
            "loop-root-future", "LOOP root greater than the PR number is invalid"
        )
    if root == number:
        if not _has_prior_new_root_proof(api, number):
            raise ReleaseClassificationBlocked(
                "loop-new-proof-missing",
                "LOOP retry cannot create an independent identity without prior repo-owned proof",
            )
        values = {"head": head_sha, "pr": number, "root": root}
        if not _has_comment_proof(api, number, NEW_ROOT_PROOF_MARKER, **values):
            api.add_comment(
                number,
                f"Release Train rebound the existing independent LOOP identity to fixed exact "
                f"head `{head_sha}`; class, scope, and root are unchanged.\n\n"
                + _proof_marker(NEW_ROOT_PROOF_MARKER, **values),
            )
        return "new"

    if _terminal_loop_member(api, root) is not None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-root-terminal", "terminal LOOP root cannot be retried or reactivated"
        )
    gate, gate_root = _validated_ui_gate(api, expected_root=root)
    gate_number = int(gate.get("number") or 0)
    if gate_root != root or not _has_prior_recovery_proof(
        api,
        number,
        gate=gate_number,
        root=root,
    ):
        raise ReleaseClassificationBlocked(
            "loop-recovery-proof-missing",
            "LOOP retry cannot create recovery identity without prior exact gate/root proof",
        )
    values = {"gate": gate_number, "head": head_sha, "pr": number, "root": root}
    if not _has_comment_proof(api, number, RECOVERY_PROOF_MARKER, **values):
        api.add_comment(
            number,
            f"Release Train rebound the existing LOOP recovery identity to fixed exact head "
            f"`{head_sha}`; gate #{gate_number}, class, scope, and root are unchanged.\n\n"
            + _proof_marker(RECOVERY_PROOF_MARKER, **values),
        )
    return "recovery"


def _validate_gate_identity(api: ReleaseApi, gate_number: int, root: int) -> dict[str, Any]:
    gate = api.get_pull(gate_number)
    gate_labels = label_names(gate)
    merge_sha = str(gate.get("merge_commit_sha") or "").lower()
    if (
        not bool(gate.get("merged"))
        or task_class_from_labels(gate_labels) != LOOP_TASK_LABEL
        or scope_from_labels(gate_labels) != LIVE_RUNTIME_LABEL
        or loop_root_from_labels(gate_labels) != root
        or not _has_comment_proof(
            api,
            gate_number,
            DEPLOY_PROOF_MARKER,
            merge=merge_sha,
            pr=gate_number,
            root=root,
        )
    ):
        raise ReleaseClassificationBlocked(
            "loop-recovery-gate-unproven",
            "LOOP recovery gate lacks repo-owned exact deploy/root proof",
        )
    return gate


def _validated_ui_gate(
    api: ReleaseApi,
    *,
    expected_gate: int | None = None,
    expected_root: int | None = None,
) -> tuple[dict[str, Any], int]:
    gate_item = _active_gate(api, AWAITING_UI_LABEL)
    if gate_item is None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-gate-missing",
            "LOOP recovery requires an active release:awaiting-ui gate",
        )
    gate_number = int(gate_item.get("number") or 0)
    if expected_gate is not None and gate_number != expected_gate:
        raise ReleaseClassificationBlocked(
            "loop-recovery-gate-stale", "LOOP recovery gate PR is stale"
        )
    gate = api.get_pull(gate_number)
    gate_labels = label_names(gate)
    gate_root = loop_root_from_labels(gate_labels)
    if gate_root is None or (expected_root is not None and gate_root != expected_root):
        raise ReleaseClassificationBlocked(
            "loop-recovery-root-mismatch", "LOOP recovery root does not match the active UI gate"
        )
    return _validate_gate_identity(api, gate_number, gate_root), gate_root


def _validate_active_ui_gate_registration(
    api: ReleaseApi,
    gate_item: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    gate_number = int(gate_item.get("number") or 0)
    gate, root = _validated_ui_gate(api, expected_gate=gate_number)
    loop_registration_kind(api, gate)
    if _terminal_loop_member(api, root) is not None:
        raise ReleaseClassificationBlocked(
            "loop-gate-root-terminal", "active UI gate references a terminal LOOP root"
        )
    return gate, root


def _loop_registration_proof_kind(
    api: ReleaseApi,
    pull: Mapping[str, Any],
    *,
    require_active_recovery: bool,
    allow_terminal: bool = False,
) -> str:
    """Validate immutable enrollment proof, optionally requiring the live recovery gate."""

    number = int(pull.get("number") or 0)
    labels = label_names(pull)
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseClassificationBlocked(
            "loop-class-scope-invalid", "LOOP identity requires task:loop + scope:live-runtime"
        )
    if labels & TERMINAL_LABELS and not allow_terminal:
        raise ReleaseClassificationBlocked(
            "loop-terminal-pr", "terminal LOOP identity cannot be resumed or inherited"
        )
    try:
        root = loop_root_from_labels(labels)
    except ReleaseBlocked as exc:
        raise ReleaseClassificationBlocked("loop-root-ambiguous", str(exc)) from exc
    if root is None:
        raise ReleaseClassificationBlocked(
            "loop-root-missing", "LOOP PR lacks repo-owned new/recovery registration"
        )
    if root > number:
        raise ReleaseClassificationBlocked(
            "loop-root-future", "LOOP root greater than the PR number is invalid"
        )
    head_sha = str((pull.get("head") or {}).get("sha") or "").lower()
    if root == number:
        if not _has_comment_proof(
            api,
            number,
            NEW_ROOT_PROOF_MARKER,
            head=head_sha,
            pr=number,
            root=root,
        ):
            raise ReleaseClassificationBlocked(
                "loop-new-proof-missing",
                "independent LOOP root lacks repo-owned exact-head new-root proof",
            )
        return "new"
    proof_gate = _recovery_proof_gate(api, number, head_sha=head_sha, root=root)
    if proof_gate is None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-proof-missing",
            "LOOP recovery lacks repo-owned exact head/gate/root proof",
        )
    _validate_gate_identity(api, proof_gate, root)
    if not require_active_recovery:
        return "recovery"
    if _terminal_loop_member(api, root) is not None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-root-terminal", "terminal LOOP root cannot be reactivated"
        )
    active_item = _active_gate(api, AWAITING_UI_LABEL)
    if active_item is None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-gate-missing",
            "LOOP recovery requires an active release:awaiting-ui gate",
        )
    gate_number = int(active_item.get("number") or 0)
    if gate_number == number and AWAITING_UI_LABEL in labels:
        gate_root = loop_root_from_labels(labels)
    else:
        gate, gate_root = _validated_ui_gate(api, expected_root=root)
        gate_number = int(gate.get("number") or 0)
    if gate_root != root or (
        gate_number != proof_gate
        and not (gate_number == number and AWAITING_UI_LABEL in labels)
    ):
        raise ReleaseClassificationBlocked(
            "loop-recovery-gate-stale", "LOOP recovery proof no longer matches the active gate"
        )
    return "recovery"


def loop_registration_kind(api: ReleaseApi, pull: Mapping[str, Any]) -> str:
    """Return new/recovery only for an exact, currently valid repo-owned identity proof."""

    return _loop_registration_proof_kind(
        api,
        pull,
        require_active_recovery=True,
    )


def _registered_ready_labels(labels: Iterable[str], root: int) -> set[str]:
    result = set(labels)
    result -= STATE_LABELS | {NEEDS_RESUME_LABEL}
    result -= _loop_root_labels(result) | loop_ack_labels(result)
    result.update({READY_LABEL, loop_root_label(root)})
    assert_state_invariants(result)
    return result


def _ensure_loop_root_label(api: ReleaseApi, root: int) -> None:
    api.ensure_label(
        loop_root_label(root),
        "C2A5F8",
        f"Deterministic recovery chain for LOOP PR #{root}",
    )


def _exact_open_loop_identity(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    association: str,
) -> tuple[dict[str, Any], set[str], str]:
    """Read exact identity for a delayed idempotent command without changing its state."""

    _require_loop_operator(association)
    pull = api.get_pull(number)
    labels = label_names(pull)
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")) or bool(pull.get("merged")):
        raise ReleaseClassificationBlocked(
            "loop-pr-not-open", "LOOP enrollment requires an open, unmerged, non-draft PR"
        )
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseClassificationBlocked(
            "loop-class-scope-invalid", "LOOP enrollment requires task:loop + scope:live-runtime"
        )
    if labels & TERMINAL_LABELS:
        raise ReleaseClassificationBlocked(
            "loop-terminal-pr", "terminal release state cannot be enrolled or inherited"
        )
    return pull, labels, _exact_head(pull, expected_head_sha)


def enqueue_loop_new(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    actor: str,
    association: str,
    check_name: str = "baseline",
) -> str:
    """Register one independent LOOP root and enqueue it in one label replacement."""

    _, existing_labels, existing_head = _exact_open_loop_identity(
        api,
        number,
        expected_head_sha,
        association=association,
    )
    existing_root = loop_root_from_labels(existing_labels)
    if existing_root == number and _has_comment_proof(
        api,
        number,
        NEW_ROOT_PROOF_MARKER,
        head=existing_head,
        pr=number,
        root=number,
    ):
        existing_state = release_state_from_labels(existing_labels)
        if existing_state in {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, BLOCKED_LABEL}:
            if not (
                existing_state == BLOCKED_LABEL
                and _classification_blocker_unresolved(api, number)
            ):
                return "already-enqueued-new"
    _, labels, head_sha = _require_enrollable_loop(
        api,
        number,
        expected_head_sha,
        association=association,
        check_name=check_name,
    )
    current_root = loop_root_from_labels(labels)
    if current_root not in {None, number}:
        raise ReleaseClassificationBlocked(
            "loop-root-correction-required",
            "existing LOOP root differs from this PR; use evidence-bound identity correction",
        )
    proof_values = {"head": head_sha, "pr": number, "root": number}
    proven = _has_comment_proof(api, number, NEW_ROOT_PROOF_MARKER, **proof_values)
    unresolved = _classification_blocker_unresolved(api, number)
    desired = _registered_ready_labels(labels, number)
    if proven and not unresolved and desired == labels:
        return "already-enqueued-new"
    if not proven or unresolved:
        api.add_comment(
            number,
            f"Release Train registered independent LOOP root #{number} for exact head `{head_sha}` "
            f"by @{actor}.\n\n{_proof_marker(NEW_ROOT_PROOF_MARKER, **proof_values)}",
        )
    _ensure_loop_root_label(api, number)
    api.set_labels(number, sorted(desired))
    api.dispatch_workflow("release-train.yml", "main")
    return "enqueued-new"


def enqueue_loop_recovery(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    gate_pr: int,
    expected_root: int,
    actor: str,
    association: str,
    check_name: str = "baseline",
) -> str:
    """Register recovery only against the exact active awaiting-ui gate/root."""

    if expected_root >= number:
        raise ReleaseClassificationBlocked(
            "loop-recovery-root-order", "recovery root must be lower than the recovery PR number"
        )
    existing_pull, existing_labels, existing_head = _exact_open_loop_identity(
        api,
        number,
        expected_head_sha,
        association=association,
    )
    if loop_root_from_labels(existing_labels) == expected_root and _has_comment_proof(
        api,
        number,
        RECOVERY_PROOF_MARKER,
        gate=gate_pr,
        head=existing_head,
        pr=number,
        root=expected_root,
    ):
        existing_state = release_state_from_labels(existing_labels)
        if existing_state in {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, BLOCKED_LABEL}:
            if not (
                existing_state == BLOCKED_LABEL
                and _classification_blocker_unresolved(api, number)
            ):
                loop_registration_kind(api, existing_pull)
                return "already-enqueued-recovery"
    _, labels, head_sha = _require_enrollable_loop(
        api,
        number,
        expected_head_sha,
        association=association,
        check_name=check_name,
    )
    current_root = loop_root_from_labels(labels)
    if current_root not in {None, expected_root}:
        raise ReleaseClassificationBlocked(
            "loop-root-correction-required",
            "existing LOOP root differs from expected recovery root",
        )
    if _terminal_loop_member(api, expected_root) is not None:
        raise ReleaseClassificationBlocked(
            "loop-recovery-root-terminal", "terminal LOOP root cannot be reactivated"
        )
    _validated_ui_gate(api, expected_gate=gate_pr, expected_root=expected_root)
    proof_values = {
        "gate": gate_pr,
        "head": head_sha,
        "pr": number,
        "root": expected_root,
    }
    proven = _has_comment_proof(api, number, RECOVERY_PROOF_MARKER, **proof_values)
    unresolved = _classification_blocker_unresolved(api, number)
    desired = _registered_ready_labels(labels, expected_root)
    if proven and not unresolved and desired == labels:
        return "already-enqueued-recovery"
    if not proven or unresolved:
        api.add_comment(
            number,
            f"Release Train registered LOOP recovery #{number} for gate #{gate_pr}, root "
            f"#{expected_root}, exact head `{head_sha}` by @{actor}.\n\n"
            + _proof_marker(RECOVERY_PROOF_MARKER, **proof_values),
        )
    _ensure_loop_root_label(api, expected_root)
    api.set_labels(number, sorted(desired))
    api.dispatch_workflow("release-train.yml", "main")
    return "enqueued-recovery"


def correct_loop_identity_to_new(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    *,
    expected_old_root: int,
    actor: str,
    association: str,
    check_name: str = "baseline",
) -> str:
    """Replace one proven stale terminal recovery link with an independent identity."""

    if association.upper() not in {"OWNER", "MEMBER"}:
        raise ReleaseBlocked("LOOP identity correction requires repository OWNER or MEMBER")
    _, existing_labels, existing_head = _exact_open_loop_identity(
        api,
        number,
        expected_head_sha,
        association=association,
    )
    correction_values = {
        "from_root": expected_old_root,
        "head": existing_head,
        "pr": number,
        "to_root": number,
    }
    if (
        loop_root_from_labels(existing_labels) == number
        and _has_comment_proof(
            api,
            number,
            IDENTITY_CORRECTION_PROOF_MARKER,
            **correction_values,
        )
        and _has_comment_proof(
            api,
            number,
            NEW_ROOT_PROOF_MARKER,
            head=existing_head,
            pr=number,
            root=number,
        )
    ):
        existing_state = release_state_from_labels(existing_labels)
        if existing_state in {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, BLOCKED_LABEL}:
            if not (
                existing_state == BLOCKED_LABEL
                and _classification_blocker_unresolved(api, number)
            ):
                return "already-corrected-to-new"
    _, labels, head_sha = _require_enrollable_loop(
        api,
        number,
        expected_head_sha,
        association=association,
        check_name=check_name,
    )
    if expected_old_root >= number:
        raise ReleaseClassificationBlocked(
            "loop-correction-root-order", "old recovery root must be lower than the PR number"
        )
    if _terminal_loop_member(api, expected_old_root) is None:
        raise ReleaseClassificationBlocked(
            "loop-correction-terminal-proof-missing",
            "identity correction requires repo-owned terminal proof for the old root",
        )
    gate = _active_gate(api, AWAITING_UI_LABEL)
    if gate is not None and loop_root_from_labels(label_names(gate)) == expected_old_root:
        raise ReleaseClassificationBlocked(
            "loop-correction-gate-active", "identity correction is forbidden while the old root gate is active"
        )
    correction_values = {
        "from_root": expected_old_root,
        "head": head_sha,
        "pr": number,
        "to_root": number,
    }
    new_values = {"head": head_sha, "pr": number, "root": number}
    corrected = _has_comment_proof(
        api,
        number,
        IDENTITY_CORRECTION_PROOF_MARKER,
        **correction_values,
    )
    unresolved = _classification_blocker_unresolved(api, number)
    current_root = loop_root_from_labels(labels)
    desired = _registered_ready_labels(labels, number)
    if corrected and not unresolved and current_root == number and desired == labels:
        return "already-corrected-to-new"
    if (
        BLOCKED_LABEL not in labels
        or not _classification_blocker_unresolved(api, number)
        or not _has_comment_proof(
            api,
            number,
            CLASSIFICATION_BLOCKER_MARKER,
            head=head_sha,
            pr=number,
        )
    ):
        raise ReleaseClassificationBlocked(
            "loop-correction-blocker-proof-missing",
            "identity correction requires the exact-head classification blocker proof",
        )
    if current_root != expected_old_root:
        raise ReleaseClassificationBlocked(
            "loop-correction-old-root-mismatch", "current LOOP root does not match expected old root"
        )
    if not corrected or unresolved:
        api.add_comment(
            number,
            f"Release Train corrected stale terminal root #{expected_old_root} to independent root "
            f"#{number} on exact head `{head_sha}` by @{actor}.\n\n"
            + _proof_marker(IDENTITY_CORRECTION_PROOF_MARKER, **correction_values)
            + "\n"
            + _proof_marker(NEW_ROOT_PROOF_MARKER, **new_values),
        )
    _ensure_loop_root_label(api, number)
    api.set_labels(number, sorted(desired))
    api.dispatch_workflow("release-train.yml", "main")
    return "corrected-to-new"


def _status_metadata(body: str) -> dict[str, Any] | None:
    prefix = f"<!-- {STATUS_COMMENT_MARKER} "
    for line in body.splitlines():
        if line.startswith(prefix) and line.endswith(" -->"):
            try:
                payload = json.loads(line[len(prefix) : -4])
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None
    return None


def upsert_status_comment(
    api: ReleaseApi,
    number: int,
    *,
    owner: str,
    reason: str,
    last_action: str,
    intervention: bool,
    now: float | None = None,
    root_override: int | None = None,
    resume_override: str | None = None,
) -> dict[str, Any]:
    """Maintain exactly one machine-owned status/heartbeat comment per active PR."""

    pull = api.get_pull(number)
    labels = label_names(pull)
    task_class = task_class_from_labels(labels)
    state = release_state_from_labels(labels)
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    root = root_override if root_override is not None else loop_root_from_labels(labels)
    root_identity = root or 0
    heartbeat = time.time() if now is None else now
    resume = "не требуется"
    if intervention:
        resume = resume_override or (
            f"`python3 apps/github_release_train_wait.py {number} --resume-owner --no-ack-agent`"
        )
    metadata = {
        "heartbeat": heartbeat,
        "head": head_sha,
        "owner": owner,
        "pr": number,
        "root": root_identity,
        "state": state,
    }
    body = "\n".join(
        (
            f"<!-- {STATUS_COMMENT_MARKER} {json.dumps(metadata, sort_keys=True)} -->",
            "### Release Train status",
            f"- Задача: {str(pull.get('title') or f'PR #{number}')}",
            f"- Класс: `{task_class}`",
            f"- Этап: `{state}`",
            f"- Ожидание: {reason}",
            f"- LOOP root: `{root_identity or '—'}`",
            f"- Последнее действие: {last_action}",
            f"- Вмешательство: {'требуется' if intervention else 'не требуется'}",
            f"- Resume: {resume}",
        )
    )
    matches = [
        item
        for item in api.list_comments(number)
        if f"<!-- {STATUS_COMMENT_MARKER} " in str(item.get("body") or "")
    ]
    if matches and int(matches[0].get("id") or 0) > 0:
        api.update_comment(int(matches[0]["id"]), body)
        for duplicate in matches[1:]:
            duplicate_id = int(duplicate.get("id") or 0)
            if duplicate_id > 0:
                api.delete_comment(duplicate_id)
    elif not matches:
        api.add_comment(number, body)
    return metadata


def mark_classification_blocked(
    api: ReleaseApi,
    number: int,
    error: ReleaseClassificationBlocked,
) -> None:
    """Persist one exact classification reason without mutating any other PR/root."""

    pull = api.get_pull(number)
    labels = label_names(pull)
    head_sha = str((pull.get("head") or {}).get("sha") or "").lower()
    values = {"head": head_sha, "pr": number}
    classification_action = "identity must be registered again; `retry-blocked` is forbidden"
    try:
        root = loop_root_from_labels(labels)
    except ReleaseBlocked:
        root = None
    if root == number:
        classification_action = (
            f"`/wb-core loop enqueue-new {number} head {head_sha}`"
        )
    elif root is not None and root < number:
        if _terminal_loop_member(api, root) is not None:
            classification_action = (
                f"`/wb-core loop correct-to-new {number} head {head_sha} old-root {root}`"
            )
        else:
            try:
                gate = _active_gate(api, AWAITING_UI_LABEL)
            except ReleaseTrainError:
                gate = None
            if gate is not None and loop_root_from_labels(label_names(gate)) == root:
                classification_action = (
                    f"`/wb-core loop enqueue-recovery {number} head {head_sha} "
                    f"gate {int(gate.get('number') or 0)} root {root}`"
                )
    proven = _classification_blocker_unresolved(api, number)
    set_release_state(
        api,
        number,
        BLOCKED_LABEL,
        current_labels=labels,
        comment=(
            f"Release Train classification error `{error.code}`: `{error}`. Generic retry is "
            "not an identity repair.\n\n"
            + _proof_marker(CLASSIFICATION_BLOCKER_MARKER, **values)
            if not proven
            else ""
        ),
    )
    upsert_status_comment(
        api,
        number,
        owner="classification-fail-closed",
        reason=f"classification error `{error.code}`: {error}",
        last_action="Release Train preserved labels on all other PRs and roots",
        intervention=True,
        root_override=root or 0,
        resume_override=classification_action,
    )


def _latest_status_heartbeat(api: ReleaseApi, number: int, head_sha: str) -> float | None:
    heartbeats: list[float] = []
    for comment in api.list_comments(number):
        metadata = _status_metadata(str(comment.get("body") or ""))
        if metadata and str(metadata.get("head") or "") == head_sha:
            try:
                heartbeats.append(float(metadata.get("heartbeat")))
            except (TypeError, ValueError):
                continue
    return max(heartbeats) if heartbeats else None


def mark_needs_resume_if_stale(
    api: ReleaseApi,
    gate: Mapping[str, Any],
    *,
    threshold_seconds: float,
    now: float | None = None,
) -> bool:
    """Mark a lost LOOP owner without acknowledging, accepting UI, or opening a gate."""

    number = int(gate.get("number") or 0)
    if number <= 0:
        raise ReleaseTrainError("active LOOP state has no PR number")
    pull = api.get_pull(number)
    labels = label_names(pull)
    state = release_state_from_labels(labels)
    resumable_states = {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, AWAITING_UI_LABEL}
    if state not in resumable_states:
        return False
    if task_class_from_labels(labels) != LOOP_TASK_LABEL:
        return False
    if scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseTrainError(
            f"active release:awaiting-agent PR #{number} is not scope:live-runtime"
        )
    if state != AWAITING_UI_LABEL and (
        str(pull.get("state") or "") != "open" or bool(pull.get("draft"))
    ):
        raise ReleaseTrainError(
            f"active LOOP PR #{number} is not an open non-draft PR"
        )
    if state == AWAITING_UI_LABEL and not bool(pull.get("merged")):
        raise ReleaseTrainError(f"release:awaiting-ui PR #{number} is not merged")
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    loop_ack_label(head_sha)
    observed_at = _latest_status_heartbeat(api, number, head_sha)
    if observed_at is None:
        observed_at = _latest_label_timestamp(
            api,
            number,
            state,
            fallback=pull,
        )
    current_time = time.time() if now is None else now
    stale = NEEDS_RESUME_LABEL in labels
    if observed_at is not None:
        stale = stale or current_time - observed_at >= threshold_seconds
    if not stale:
        return False

    if NEEDS_RESUME_LABEL not in labels:
        api.add_labels(number, [NEEDS_RESUME_LABEL])
    upsert_status_comment(
        api,
        number,
        owner="unowned",
        reason=f"owner heartbeat истёк на exact head `{head_sha}`",
        last_action="Release Train выставил release:needs-resume fail-closed",
        intervention=True,
        now=current_time,
    )
    return True


def refresh_lost_loop_owners(
    api: ReleaseApi,
    *,
    threshold_seconds: float,
    now: float | None = None,
) -> list[int]:
    """Apply the resume overlay once to every stale active LOOP owner."""

    candidates: dict[int, dict[str, Any]] = {}
    for label in (READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, AWAITING_UI_LABEL):
        for item in api.list_issues_by_label(label, state="all"):
            if "pull_request" in item:
                candidates[int(item.get("number") or 0)] = item
    stale: list[int] = []
    for number, item in sorted(candidates.items()):
        if number > 0 and mark_needs_resume_if_stale(
            api,
            item,
            threshold_seconds=threshold_seconds,
            now=now,
        ):
            stale.append(number)
    return stale


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


def queue_gate_state(api: ReleaseApi) -> dict[str, Any]:
    halted = [
        item
        for item in api.list_issues_by_label(HALTED_LABEL, state="all")
        if "pull_request" in item
    ]
    if halted:
        first = min(
            halted,
            key=lambda item: (str(item.get("created_at") or ""), int(item.get("number") or 0)),
        )
        return {"status": "halted", "pr_number": int(first.get("number") or 0)}
    try:
        agent_gate = _active_gate(api, AWAITING_AGENT_LABEL)
        ui_gate = _active_gate(api, AWAITING_UI_LABEL)
    except ReleaseTrainError as exc:
        return {"status": "gate-conflict", "reason": str(exc)}
    if agent_gate is not None:
        try:
            loop_registration_kind(
                api,
                api.get_pull(int(agent_gate.get("number") or 0)),
            )
        except ReleaseClassificationBlocked as exc:
            return {
                "status": "gate-conflict",
                "reason": f"classification error {exc.code}: {exc}",
            }
        return {
            "status": "awaiting-agent",
            "pr_number": int(agent_gate.get("number") or 0),
        }
    if ui_gate is not None:
        try:
            _, root = _validate_active_ui_gate_registration(api, ui_gate)
        except ReleaseClassificationBlocked as exc:
            return {
                "status": "gate-conflict",
                "reason": f"classification error {exc.code}: {exc}",
            }
        except (ReleaseBlocked, ReleaseTrainError) as exc:
            return {"status": "gate-conflict", "reason": str(exc)}
        return {
            "status": "awaiting-ui",
            "pr_number": int(ui_gate.get("number") or 0),
            "loop_root": root,
        }
    running = [
        item
        for item in api.list_issues_by_label(RUNNING_LABEL, state="all")
        if "pull_request" in item
    ]
    if running:
        first = min(running, key=lambda item: (str(item.get("created_at") or ""), int(item.get("number") or 0)))
        return {"status": "running", "pr_number": int(first.get("number") or 0)}
    ready = [
        item
        for item in api.list_issues_by_label(READY_LABEL, state="open")
        if "pull_request" in item and RUNNING_LABEL not in label_names(item)
    ]
    if ready:
        first = min(ready, key=lambda item: (str(item.get("created_at") or ""), int(item.get("number") or 0)))
        return {"status": "ready", "pr_number": int(first.get("number") or 0)}
    return {"status": "idle"}


def _validate_task_context(
    api: ReleaseApi,
    number: int,
    labels: Iterable[str],
    scope: str,
) -> tuple[str, int | None]:
    values = set(labels)
    task_class = task_class_from_labels(values)
    try:
        root = loop_root_from_labels(values)
    except ReleaseBlocked as exc:
        raise ReleaseClassificationBlocked("loop-root-ambiguous", str(exc)) from exc
    ui_gate = _active_gate(api, AWAITING_UI_LABEL)
    if task_class == STANDARD_TASK_LABEL:
        if root is not None or loop_ack_labels(values):
            raise ReleaseBlocked("STANDARD PR cannot carry LOOP recovery or acknowledgement labels")
        return task_class, None
    if scope != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("LOOP PR must use scope:live-runtime")
    pull = api.get_pull(number)
    registration = loop_registration_kind(api, pull)
    if registration == "new" and ui_gate is not None:
        gate_number = int(ui_gate.get("number") or 0)
        raise ReleaseBlocked(
            f"independent LOOP root waits normally behind active UI gate PR #{gate_number}"
        )
    return task_class, root


def transition_label_set(current: Iterable[str], state: str) -> set[str]:
    if state not in STATE_LABELS:
        raise ValueError(f"unknown release state label: {state}")
    labels = set(current)
    current_state = release_state_from_labels(labels)
    if not transition_allowed(current_state, state):
        raise ValueError(f"forbidden release transition: {current_state} -> {state}")
    if state == RUNNING_LABEL:
        labels -= STATE_LABELS - {READY_LABEL}
        labels.discard(NEEDS_RESUME_LABEL)
        labels.add(READY_LABEL)
        labels.add(RUNNING_LABEL)
        assert_state_invariants(labels)
        return labels
    labels -= STATE_LABELS
    if state != AWAITING_AGENT_LABEL:
        labels.discard(NEEDS_RESUME_LABEL)
    labels.add(state)
    assert_state_invariants(labels)
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
    if before != after:
        api.set_labels(number, sorted(after))
    if comment:
        api.add_comment(number, comment)


def select_candidate(
    api: ReleaseApi,
    *,
    needs_resume_after_seconds: float = DEFAULT_NEEDS_RESUME_AFTER_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    if needs_resume_after_seconds <= 0:
        raise ValueError("needs-resume threshold must be positive")
    try:
        refresh_lost_loop_owners(
            api,
            threshold_seconds=needs_resume_after_seconds,
            now=now,
        )
    except (ReleaseBlocked, ReleaseTrainError) as exc:
        return {"status": "gate-conflict", "found": False, "reason": str(exc)}
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
        agent_number = int(agent_gate.get("number") or 0)
        try:
            loop_registration_kind(api, api.get_pull(agent_number))
        except ReleaseClassificationBlocked as exc:
            mark_classification_blocked(api, agent_number, exc)
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": f"classification error {exc.code}: {exc}",
            }
        try:
            needs_resume = mark_needs_resume_if_stale(
                api,
                agent_gate,
                threshold_seconds=needs_resume_after_seconds,
                now=now,
            )
        except ReleaseTrainError as exc:
            return {"status": "gate-conflict", "found": False, "reason": str(exc)}
        return {
            "status": "awaiting-agent",
            "found": False,
            "awaiting_agent_pr_number": int(agent_gate.get("number") or 0),
            "needs_resume": needs_resume,
        }

    ready = [
        item
        for item in api.list_issues_by_label(READY_LABEL, state="open")
        if "pull_request" in item
        and BLOCKED_LABEL not in label_names(item)
        and SUPERSEDED_LABEL not in label_names(item)
    ]
    if ui_gate is not None:
        try:
            _, active_root = _validate_active_ui_gate_registration(api, ui_gate)
        except ReleaseClassificationBlocked as exc:
            gate_number = int(ui_gate.get("number") or 0)
            upsert_status_comment(
                api,
                gate_number,
                owner="classification-fail-closed",
                reason=f"classification error `{exc.code}`: {exc}",
                last_action="Release Train preserved labels on all other PRs and roots",
                intervention=True,
                root_override=0,
                resume_override="classification proof must be repaired; `retry-blocked` is forbidden",
            )
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": f"classification error {exc.code}: {exc}",
            }
        except (ReleaseBlocked, ReleaseTrainError) as exc:
            return {"status": "gate-conflict", "found": False, "reason": str(exc)}
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
        agent_acknowledged = (
            loop_ack_label(head_sha) in final_labels
            and _has_comment_proof(
                api,
                number,
                ACK_PROOF_MARKER,
                head=head_sha,
                pr=number,
            )
        )
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
        if acknowledgement not in labels or not _has_comment_proof(
            api,
            candidate.number,
            ACK_PROOF_MARKER,
            head=candidate.head_sha,
            pr=candidate.number,
        ):
            raise ReleaseBlocked(
                "repo-owned LOOP acknowledgement proof is missing or does not match the exact head SHA"
            )
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


def complete_standard_release(
    api: ReleaseApi,
    number: int,
    *,
    merge_sha: str,
    contour: str,
) -> str:
    """Complete STANDARD only from exact merge/deploy evidence owned by this command."""

    pull = api.get_pull(number)
    labels = label_names(pull)
    exact_merge = merge_sha.strip().lower()
    if not bool(pull.get("merged")) or str(pull.get("merge_commit_sha") or "").lower() != exact_merge:
        raise ReleaseBlocked("completion proof does not match the exact merged PR SHA")
    if task_class_from_labels(labels) != STANDARD_TASK_LABEL:
        raise ReleaseBlocked("STANDARD completion command requires task:standard")
    scope = scope_from_labels(labels)
    if contour == "repo-only":
        if scope != REPO_ONLY_LABEL:
            raise ReleaseBlocked("repo-only completion requires scope:repo-only")
        target = DONE_LABEL
    elif contour == "production-verified":
        if scope != LIVE_RUNTIME_LABEL:
            raise ReleaseBlocked("production completion requires scope:live-runtime")
        target = PRODUCTION_LABEL
    else:
        raise ReleaseBlocked("unsupported completion contour")
    proof = _proof_marker(
        COMPLETION_PROOF_MARKER,
        contour=contour,
        merge=exact_merge,
        pr=number,
    )
    if not _has_comment_proof(
        api,
        number,
        COMPLETION_PROOF_MARKER,
        contour=contour,
        merge=exact_merge,
        pr=number,
    ):
        api.add_comment(
            number,
            f"Release Train verified `{contour}` completion for exact merge `{exact_merge}`.\n\n{proof}",
        )
    set_release_state(api, number, target, current_labels=labels)
    return target


def halt_merged_release(
    api: ReleaseApi,
    number: int,
    *,
    merge_sha: str,
    reason: str,
) -> str:
    pull = api.get_pull(number)
    labels = label_names(pull)
    exact_merge = merge_sha.strip().lower()
    if not bool(pull.get("merged")) or str(pull.get("merge_commit_sha") or "").lower() != exact_merge:
        raise ReleaseBlocked("halt proof does not match the exact merged PR SHA")
    if scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("release:halted requires scope:live-runtime")
    proof = _proof_marker(HALT_PROOF_MARKER, merge=exact_merge, pr=number)
    if not _has_comment_proof(api, number, HALT_PROOF_MARKER, merge=exact_merge, pr=number):
        api.add_comment(
            number,
            f"Release Train halted exact merged release `{exact_merge}`: {reason}\n\n{proof}",
        )
    set_release_state(api, number, HALTED_LABEL, current_labels=labels)
    return HALTED_LABEL


def retry_blocked_release(
    api: ReleaseApi,
    number: int,
    *,
    expected_head_sha: str,
    check_name: str,
) -> str:
    """Requeue a fixed pre-merge blocker only after exact-head successful CI."""

    pull = api.get_pull(number)
    labels = label_names(pull)
    actual_head = str((pull.get("head") or {}).get("sha") or "").lower()
    if BLOCKED_LABEL not in labels:
        raise ReleaseBlocked("retry requires release:blocked")
    if actual_head != expected_head_sha.strip().lower():
        raise ReleaseBlocked("blocked retry head SHA is stale")
    if len(actual_head) != 40 or any(
        character not in "0123456789abcdef" for character in actual_head
    ):
        raise ReleaseBlocked("blocked retry requires an exact 40-character head SHA")
    classification_marker = _classification_blocker_unresolved(api, number)
    if str(pull.get("state") or "") != "open" or bool(pull.get("draft")):
        raise ReleaseBlocked("blocked retry requires an open non-draft PR")
    task_class = task_class_from_labels(labels)
    scope_from_labels(labels)
    successful_check = _has_successful_check(api, actual_head, check_name)
    if task_class == LOOP_TASK_LABEL:
        try:
            loop_registration_kind(api, pull)
        except ReleaseClassificationBlocked as exc:
            if classification_marker:
                raise ReleaseClassificationBlocked(
                    "generic-retry-classification-forbidden",
                    "retry-blocked repairs only technical pre-merge blockers and cannot change LOOP identity",
                ) from exc
            if not successful_check:
                raise ReleaseBlocked(
                    f"blocked retry requires successful {check_name!r} on exact head {actual_head}"
                ) from exc
            _refresh_loop_registration_for_retry(api, pull)
            loop_registration_kind(api, api.get_pull(number))
    if not successful_check:
        raise ReleaseBlocked(
            f"blocked retry requires successful {check_name!r} on exact head {actual_head}"
        )
    proof = _proof_marker(RETRY_PROOF_MARKER, check=check_name, head=actual_head, pr=number)
    api.add_comment(
        number,
        f"Release Train retry accepted after `{check_name}` succeeded on exact head `{actual_head}`.\n\n{proof}",
    )
    set_release_state(api, number, READY_LABEL, current_labels=labels)
    api.dispatch_workflow("release-train.yml", "main")
    return READY_LABEL


def terminal_state_proven(api: ReleaseApi, pull: Mapping[str, Any]) -> bool:
    """Reject terminal labels that lack their repo-owned exact-SHA transition proof."""

    number = int(pull.get("number") or 0)
    labels = label_names(pull)
    merge_sha = str(pull.get("merge_commit_sha") or "").lower()
    if number <= 0 or len(merge_sha) != 40:
        return False
    task_class = task_class_from_labels(labels)
    if task_class == STANDARD_TASK_LABEL:
        scope = scope_from_labels(labels)
        contour = "repo-only" if scope == REPO_ONLY_LABEL else "production-verified"
        expected = DONE_LABEL if scope == REPO_ONLY_LABEL else PRODUCTION_LABEL
        completion = _has_comment_proof(
            api,
            number,
            COMPLETION_PROOF_MARKER,
            contour=contour,
            merge=merge_sha,
            pr=number,
        )
        reconciled = scope == LIVE_RUNTIME_LABEL and _has_comment_proof(
            api,
            number,
            RECONCILE_PROOF_MARKER,
            merge=merge_sha,
            pr=number,
        )
        return expected in labels and (completion or reconciled)
    if task_class == LOOP_TASK_LABEL and PRODUCTION_LABEL in labels:
        root = loop_root_from_labels(labels)
        return root is not None and _has_comment_proof(
            api,
            number,
            CHAIN_AUDIT_MARKER,
            merge=merge_sha,
            root=root,
            terminal_pr=number,
        )
    return False


def resume_halted_release(
    api: ReleaseApi,
    number: int,
    evidence: Mapping[str, Any],
) -> str:
    """Remove halted only after canonical exact-SHA reconciliation evidence."""

    pull = api.get_pull(number)
    labels = label_names(pull)
    merge_sha = str(pull.get("merge_commit_sha") or "").lower()
    head_sha = str((pull.get("head") or {}).get("sha") or "").lower()
    if not bool(pull.get("merged")):
        raise ReleaseBlocked("reconciliation applies only to a merged PR")
    required = {
        "healthy": True,
        "pr": number,
        "head": head_sha,
        "merge": merge_sha,
        "expected_sha": merge_sha,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
    }
    mismatches = [key for key, value in required.items() if evidence.get(key) != value]
    if mismatches or evidence.get("status") != "reconciled":
        raise ReleaseBlocked(
            "halted reconciliation evidence does not match exact PR/head/merge/target: "
            + ", ".join(mismatches or ["status"])
        )
    proof = _proof_marker(RECONCILE_PROOF_MARKER, merge=merge_sha, pr=number)
    already_proven = _has_comment_proof(
        api,
        number,
        RECONCILE_PROOF_MARKER,
        merge=merge_sha,
        pr=number,
    )
    if HALTED_LABEL not in labels:
        if already_proven and (PRODUCTION_LABEL in labels or AWAITING_UI_LABEL in labels):
            return "already-reconciled"
        raise ReleaseBlocked("reconciliation applies only to release:halted or its proven result")
    if not already_proven:
        api.add_comment(
            number,
            f"Release Train reconciled canonical production target at exact SHA `{merge_sha}`.\n\n{proof}",
        )
    task_class = task_class_from_labels(labels)
    if task_class == STANDARD_TASK_LABEL:
        if scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
            raise ReleaseBlocked("halted STANDARD reconciliation requires scope:live-runtime")
        set_release_state(api, number, PRODUCTION_LABEL, current_labels=labels)
        return "production"
    if task_class == LOOP_TASK_LABEL:
        _, status = mark_loop_awaiting_ui(api, number, merge_sha)
        return status
    raise ReleaseBlocked("unsupported halted task class")


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
    if NEEDS_RESUME_LABEL in labels:
        raise ReleaseBlocked(
            "LOOP owner must resume the exact head/root before acknowledgement"
        )
    acknowledgement = loop_ack_label(actual_head_sha)
    proof = _proof_marker(ACK_PROOF_MARKER, head=actual_head_sha, pr=number)
    if (
        acknowledgement in labels
        and READY_LABEL in labels
        and _has_comment_proof(api, number, ACK_PROOF_MARKER, head=actual_head_sha, pr=number)
    ):
        if NEEDS_RESUME_LABEL in labels:
            api.remove_label(number, NEEDS_RESUME_LABEL)
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
    api.ensure_label(
        acknowledgement,
        "6F42C1",
        f"One-shot LOOP agent acknowledgement for exact head {actual_head_sha}",
    )
    api.add_labels(number, [acknowledgement])
    api.add_comment(
        number,
        f"Release Train recorded exact-head acknowledgement from @{actor}.\n\n{proof}",
    )
    set_release_state(
        api,
        number,
        READY_LABEL,
        current_labels=(labels - loop_ack_labels(labels)) | {acknowledgement},
        comment=f"LOOP acknowledgement от @{actor} принят для exact head `{actual_head_sha}`.",
    )
    api.dispatch_workflow("release-train.yml", "main")
    return "acknowledged"


def resume_loop_owner(
    api: ReleaseApi,
    number: int,
    expected_head_sha: str,
    expected_root: int,
    *,
    actor: str,
    association: str,
) -> str:
    """Claim a lost LOOP session without acknowledging or accepting any safety gate."""

    if association.upper() not in LOOP_ACCEPT_ASSOCIATIONS:
        raise ReleaseBlocked("LOOP resume requires repository write association")
    pull = api.get_pull(number)
    labels = label_names(pull)
    actual_head = str((pull.get("head") or {}).get("sha") or "")
    actual_root = loop_root_from_labels(labels)
    loop_registration_kind(api, pull)
    if actual_head != expected_head_sha:
        raise ReleaseBlocked("LOOP resume head SHA is stale")
    if actual_root is None or actual_root != expected_root:
        raise ReleaseBlocked("LOOP resume root is stale or ambiguous")
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope_from_labels(labels) != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("LOOP resume requires task:loop + scope:live-runtime")
    state = release_state_from_labels(labels)
    if state not in {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, AWAITING_UI_LABEL}:
        raise ReleaseBlocked("LOOP PR is not in a resumable active state")
    if NEEDS_RESUME_LABEL in labels:
        api.remove_label(number, NEEDS_RESUME_LABEL)
    upsert_status_comment(
        api,
        number,
        owner=actor,
        reason="active owner heartbeat restored; safety gate remains unchanged",
        last_action=f"@{actor} resumed exact head `{actual_head}`",
        intervention=False,
    )
    return "resumed"


def mark_loop_awaiting_ui(api: ReleaseApi, number: int, merge_sha: str) -> tuple[int, str]:
    pull = api.get_pull(number)
    labels = label_names(pull)
    if not bool(pull.get("merged")):
        raise ReleaseBlocked("LOOP UI gate can be opened only after merge")
    if len(merge_sha) != 40 or any(character not in "0123456789abcdef" for character in merge_sha.lower()):
        raise ReleaseBlocked("LOOP UI gate requires an exact 40-character deployed merge SHA")
    if str(pull.get("merge_commit_sha") or "").lower() != merge_sha.lower():
        raise ReleaseBlocked("deployed SHA does not match the PR merge SHA")
    scope = scope_from_labels(labels)
    if task_class_from_labels(labels) != LOOP_TASK_LABEL or scope != LIVE_RUNTIME_LABEL:
        raise ReleaseBlocked("release:awaiting-ui requires task:loop + scope:live-runtime")
    current_root = loop_root_from_labels(labels)
    if current_root is None:
        raise ReleaseClassificationBlocked(
            "loop-root-missing", "LOOP UI gate requires a registered root"
        )
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
        head_sha = str((pull.get("head") or {}).get("sha") or "").lower()
        if current_root == number:
            if not _has_comment_proof(
                api,
                number,
                NEW_ROOT_PROOF_MARKER,
                head=head_sha,
                pr=number,
                root=number,
            ):
                raise ReleaseClassificationBlocked(
                    "loop-new-proof-missing",
                    "active independent LOOP gate lacks new-root proof",
                )
        else:
            proof_gate = _recovery_proof_gate(
                api,
                number,
                head_sha=head_sha,
                root=current_root,
            )
            if proof_gate is None:
                raise ReleaseClassificationBlocked(
                    "loop-recovery-proof-missing",
                    "active recovery LOOP gate lacks recovery proof",
                )
            _validate_gate_identity(api, proof_gate, current_root)
        if not _has_comment_proof(
            api,
            number,
            DEPLOY_PROOF_MARKER,
            merge=merge_sha.lower(),
            pr=number,
            root=current_root,
        ):
            api.add_comment(
                number,
                "Release Train healed the exact deployed-SHA proof for an existing UI gate.\n\n"
                + _proof_marker(
                    DEPLOY_PROOF_MARKER,
                    merge=merge_sha.lower(),
                    pr=number,
                    root=current_root,
                ),
            )
        for item in gates:
            previous = int(item.get("number") or 0)
            if previous != number:
                api.remove_label(previous, AWAITING_UI_LABEL)
                api.add_comment(previous, f"Duplicate gate healed in favor of LOOP PR #{number}.")
        return current_root, "already-awaiting-ui"
    registration = loop_registration_kind(api, pull)
    if gates and RUNNING_LABEL not in labels:
        root = next(iter(gate_roots))
        if current_root == root:
            return current_root, "superseded-iteration"
    if not gates:
        if registration != "new" or current_root != number:
            raise ReleaseBlocked("recovery LOOP lost its active parent gate before UI handoff")
        root = number
    else:
        root = next(iter(gate_roots))
        if registration != "recovery" or root <= 0 or current_root != root:
            raise ReleaseBlocked("recovery LOOP does not match the active deterministic loop root")
    root_label = loop_root_label(root)
    deploy_proof = _proof_marker(
        DEPLOY_PROOF_MARKER,
        merge=merge_sha.lower(),
        pr=number,
        root=root,
    )
    if not _has_comment_proof(
        api,
        number,
        DEPLOY_PROOF_MARKER,
        merge=merge_sha.lower(),
        pr=number,
        root=root,
    ):
        api.add_comment(
            number,
            f"Release Train verified deployed exact merge SHA `{merge_sha}`.\n\n{deploy_proof}",
        )
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


def normalize_completed_loop_chain(
    api: ReleaseApi,
    number: int,
    *,
    actor: str,
) -> list[int]:
    """Finalize merged iterations and hide unmerged predecessors of one proven LOOP root."""

    active_pull = api.get_pull(number)
    active_labels = label_names(active_pull)
    root = loop_root_from_labels(active_labels)
    if (
        root is None
        or task_class_from_labels(active_labels) != LOOP_TASK_LABEL
        or scope_from_labels(active_labels) != LIVE_RUNTIME_LABEL
        or not bool(active_pull.get("merged"))
    ):
        raise ReleaseBlocked("accepted PR is not a merged member of a deterministic LOOP chain")
    chain_label = loop_root_label(root)
    chain = [
        item
        for item in api.list_issues_by_label(chain_label, state="all")
        if "pull_request" in item
    ]
    if not chain:
        raise ReleaseTrainError("active LOOP chain is empty")

    merged_members: list[tuple[int, set[str]]] = []
    superseded_members: list[tuple[int, set[str]]] = []
    for item in sorted(chain, key=lambda candidate: int(candidate.get("number") or 0)):
        chain_number = int(item.get("number") or 0)
        chain_pull = api.get_pull(chain_number)
        chain_labels = label_names(chain_pull)
        try:
            valid_member = (
                task_class_from_labels(chain_labels) == LOOP_TASK_LABEL
                and scope_from_labels(chain_labels) == LIVE_RUNTIME_LABEL
                and loop_root_from_labels(chain_labels) == root
            )
            if valid_member:
                _loop_registration_proof_kind(
                    api,
                    chain_pull,
                    require_active_recovery=False,
                    allow_terminal=True,
                )
        except ReleaseBlocked:
            valid_member = False
        if not valid_member:
            raise ReleaseTrainError(
                f"ambiguous LOOP chain membership for PR #{chain_number}; cleanup refused"
            )
        if bool(chain_pull.get("merged")):
            merged_members.append((chain_number, chain_labels))
            continue
        if chain_number > number:
            raise ReleaseTrainError(
                f"unmerged LOOP PR #{chain_number} is newer than terminal PR #{number}; cleanup refused"
            )
        if 0 < chain_number < number:
            superseded_members.append((chain_number, chain_labels))

    if number not in {chain_number for chain_number, _ in merged_members}:
        raise ReleaseTrainError(f"active LOOP PR #{number} is absent from its root chain")

    terminal_sha = str(active_pull.get("merge_commit_sha") or "")
    audit = _proof_marker(
        CHAIN_AUDIT_MARKER,
        merge=terminal_sha,
        root=root,
        terminal_pr=number,
    )
    first_acceptance = PRODUCTION_LABEL not in active_labels
    set_release_state(
        api,
        number,
        PRODUCTION_LABEL,
        current_labels=active_labels,
        comment=(
            f"Production UI Flow принят @{actor}; LOOP-цепочка `{chain_label}` "
            f"завершена terminal PR #{number} / `{terminal_sha}`.\n\n{audit}"
            if first_acceptance
            else ""
        ),
    )
    for chain_number, chain_labels in merged_members:
        if chain_number == number:
            continue
        historical_labels = chain_labels - (
            STATE_LABELS | {NEEDS_RESUME_LABEL} | loop_ack_labels(chain_labels)
        )
        if historical_labels != chain_labels:
            api.set_labels(chain_number, sorted(historical_labels))
        if not _has_comment_proof(
            api,
            chain_number,
            CHAIN_AUDIT_MARKER,
            merge=terminal_sha,
            root=root,
            terminal_pr=number,
        ):
            api.add_comment(
                chain_number,
                f"LOOP chain closed by terminal PR #{number} / `{terminal_sha}`; historical merged "
                f"iteration retained without a false terminal production label.\n\n{audit}",
            )

    normalized: list[int] = []
    for chain_number, chain_labels in superseded_members:
        was_superseded = SUPERSEDED_LABEL in chain_labels
        set_release_state(
            api,
            chain_number,
            SUPERSEDED_LABEL,
            current_labels=chain_labels - loop_ack_labels(chain_labels),
            comment=(
                f"Незамёрженный LOOP PR #{chain_number} заменён production recovery PR #{number} "
                f"в цепочке `{chain_label}`; активные queue/failure labels нормализованы, "
                "история и root-label сохранены."
                if not was_superseded
                else ""
            ),
        )
        if str(api.get_pull(chain_number).get("state") or "") == "open":
            api.close_pull(chain_number)
        normalized.append(chain_number)
    return normalized


def accept_loop_ui(
    api: ReleaseApi,
    number: int,
    *,
    actor: str,
    association: str,
    deployed_sha: str,
    evidence: str,
) -> str:
    if association.upper() not in LOOP_ACCEPT_ASSOCIATIONS:
        raise ReleaseBlocked("LOOP UI acceptance requires repository write association")
    pull = api.get_pull(number)
    labels = label_names(pull)
    root = loop_root_from_labels(labels)
    if root is None or task_class_from_labels(labels) != LOOP_TASK_LABEL:
        raise ReleaseBlocked("PR is not part of a deterministic LOOP chain")
    merge_sha = str(pull.get("merge_commit_sha") or "").lower()
    if deployed_sha.lower() != merge_sha or len(merge_sha) != 40:
        raise ReleaseBlocked("UI acceptance must bind the current exact deployed merge SHA")
    normalized_evidence = evidence.lower()
    if not normalized_evidence.startswith("sha256:") or len(normalized_evidence) != 71:
        raise ReleaseBlocked("UI acceptance requires a sha256 evidence fingerprint")
    if any(character not in "0123456789abcdef" for character in normalized_evidence[7:]):
        raise ReleaseBlocked("UI acceptance evidence fingerprint is invalid")
    if not _has_comment_proof(
        api,
        number,
        DEPLOY_PROOF_MARKER,
        merge=merge_sha,
        pr=number,
        root=root,
    ):
        raise ReleaseBlocked("repo-owned deployed-SHA proof is missing")
    active_gate = _active_gate(api, AWAITING_UI_LABEL)
    if active_gate is None:
        if PRODUCTION_LABEL not in labels:
            raise ReleaseBlocked("there is no active release:awaiting-ui gate")
        normalize_completed_loop_chain(api, number, actor=actor)
        evidence_comment = f"UI evidence accepted for `{merge_sha}`: `{normalized_evidence}`."
        if not any(
            evidence_comment == str(item.get("body") or "").strip()
            for item in api.list_comments(number)
        ):
            api.add_comment(number, evidence_comment)
        api.dispatch_workflow("release-train.yml", "main")
        return "already-accepted"
    if int(active_gate.get("number") or 0) != number:
        raise ReleaseBlocked("UI acceptance must target the current LOOP iteration")
    normalize_completed_loop_chain(api, number, actor=actor)
    evidence_comment = f"UI evidence accepted for `{merge_sha}`: `{normalized_evidence}`."
    if not any(
        evidence_comment == str(item.get("body") or "").strip()
        for item in api.list_comments(number)
    ):
        api.add_comment(number, evidence_comment)
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
    if len(parts) == 6 and parts[:3] == ["/wb-core", "loop", "enqueue-new"]:
        try:
            command_number = int(parts[3])
        except ValueError as exc:
            raise ReleaseBlocked("invalid new LOOP PR number") from exc
        if command_number != number or parts[4] != "head":
            raise ReleaseBlocked("new LOOP enrollment must bind the current PR and exact head")
        return enqueue_loop_new(
            api,
            number,
            parts[5],
            actor=actor,
            association=association,
        )
    if len(parts) == 10 and parts[:3] == ["/wb-core", "loop", "enqueue-recovery"]:
        try:
            command_number = int(parts[3])
            gate_pr = int(parts[7])
            expected_root = int(parts[9])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP recovery identity") from exc
        if (
            command_number != number
            or parts[4] != "head"
            or parts[6] != "gate"
            or parts[8] != "root"
        ):
            raise ReleaseBlocked(
                "LOOP recovery enrollment must bind current PR, exact head, gate and root"
            )
        return enqueue_loop_recovery(
            api,
            number,
            parts[5],
            gate_pr=gate_pr,
            expected_root=expected_root,
            actor=actor,
            association=association,
        )
    if len(parts) == 8 and parts[:3] == ["/wb-core", "loop", "correct-to-new"]:
        try:
            command_number = int(parts[3])
            old_root = int(parts[7])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP correction identity") from exc
        if command_number != number or parts[4] != "head" or parts[6] != "old-root":
            raise ReleaseBlocked(
                "LOOP identity correction must bind current PR, exact head and old root"
            )
        return correct_loop_identity_to_new(
            api,
            number,
            parts[5],
            expected_old_root=old_root,
            actor=actor,
            association=association,
        )
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
    if len(parts) == 8 and parts[:3] == ["/wb-core", "loop", "resume-owner"]:
        try:
            command_number = int(parts[3])
            command_root = int(parts[7])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP resume identity") from exc
        if command_number != number or parts[4] != "head" or parts[6] != "root":
            raise ReleaseBlocked("LOOP resume must bind current PR, exact head and loop root")
        return resume_loop_owner(
            api,
            number,
            parts[5],
            command_root,
            actor=actor,
            association=association,
        )
    if len(parts) == 8 and parts[:3] == ["/wb-core", "loop", "accept-ui"]:
        try:
            command_number = int(parts[3])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP acceptance PR number") from exc
        if command_number != number or parts[4] != "deployed" or parts[6] != "evidence":
            raise ReleaseBlocked(
                "LOOP UI acceptance must bind current PR, deployed SHA and evidence"
            )
        return accept_loop_ui(
            api,
            number,
            actor=actor,
            association=association,
            deployed_sha=parts[5],
            evidence=parts[7],
        )
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
    ensure_ca_bundle()
    return GitHubApi(
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        token=os.environ.get("GITHUB_TOKEN", ""),
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )


def _json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _needs_resume_default_minutes() -> float:
    configured = os.environ.get("WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES", "").strip()
    if not configured:
        return DEFAULT_NEEDS_RESUME_AFTER_SECONDS / 60
    try:
        minutes = float(configured)
    except ValueError as exc:
        raise ValueError(
            "WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES must be a positive number"
        ) from exc
    if minutes <= 0:
        raise ValueError("WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES must be positive")
    return minutes


def command_setup(_: argparse.Namespace) -> int:
    api = _api_from_env()
    for name, (color, description) in LABEL_DEFINITIONS.items():
        api.ensure_label(name, color, description)
    _json_print({"status": "ready", "label_count": len(LABEL_DEFINITIONS)})
    return 0


def command_select(args: argparse.Namespace) -> int:
    api = _api_from_env()
    result = select_candidate(
        api,
        needs_resume_after_seconds=args.needs_resume_after_minutes * 60,
    )
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
            "needs_resume": bool(result.get("needs_resume")),
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
    if args.state in {
        DONE_LABEL,
        PRODUCTION_LABEL,
        AWAITING_UI_LABEL,
        READY_LABEL,
        HALTED_LABEL,
        SUPERSEDED_LABEL,
    }:
        raise ReleaseBlocked(
            f"critical transition to {args.state} requires its repo-owned evidence command"
        )
    set_release_state(api, args.pr, args.state, comment=args.comment or "")
    _json_print({"status": args.state, "pr_number": args.pr})
    return 0


def command_halt(args: argparse.Namespace) -> int:
    api = _api_from_env()
    status = halt_merged_release(
        api,
        args.pr,
        merge_sha=args.merge_sha,
        reason=args.reason,
    )
    _json_print({"status": status, "pr_number": args.pr, "merge_sha": args.merge_sha})
    return 0


def command_retry_blocked(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        status = retry_blocked_release(
            api,
            args.pr,
            expected_head_sha=args.expected_head_sha,
            check_name=args.check_name,
        )
    except ReleaseClassificationBlocked as exc:
        _json_print(
            {
                "status": "classification-rejected",
                "pr_number": args.pr,
                "code": exc.code,
                "reason": str(exc),
            }
        )
        return 2
    _json_print({"status": status, "pr_number": args.pr, "head_sha": args.expected_head_sha})
    return 0


def command_enqueue_loop_new(args: argparse.Namespace) -> int:
    api = _api_from_env()
    status = enqueue_loop_new(
        api,
        args.pr,
        args.expected_head_sha,
        actor=args.actor,
        association=args.association,
        check_name=args.check_name,
    )
    _json_print({"status": status, "pr_number": args.pr, "loop_root": args.pr})
    return 0


def command_enqueue_loop_recovery(args: argparse.Namespace) -> int:
    api = _api_from_env()
    status = enqueue_loop_recovery(
        api,
        args.pr,
        args.expected_head_sha,
        gate_pr=args.gate_pr,
        expected_root=args.expected_root,
        actor=args.actor,
        association=args.association,
        check_name=args.check_name,
    )
    _json_print({"status": status, "pr_number": args.pr, "loop_root": args.expected_root})
    return 0


def command_correct_loop_identity(args: argparse.Namespace) -> int:
    api = _api_from_env()
    status = correct_loop_identity_to_new(
        api,
        args.pr,
        args.expected_head_sha,
        expected_old_root=args.expected_old_root,
        actor=args.actor,
        association=args.association,
        check_name=args.check_name,
    )
    _json_print({"status": status, "pr_number": args.pr, "loop_root": args.pr})
    return 0


def command_complete(args: argparse.Namespace) -> int:
    api = _api_from_env()
    status = complete_standard_release(
        api,
        args.pr,
        merge_sha=args.merge_sha,
        contour=args.contour,
    )
    _json_print({"status": status, "pr_number": args.pr, "merge_sha": args.merge_sha})
    return 0


def command_resume_halted(args: argparse.Namespace) -> int:
    api = _api_from_env()
    payload = json.loads(args.evidence_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReleaseBlocked("reconciliation evidence must be a JSON object")
    status = resume_halted_release(api, args.pr, payload)
    _json_print({"status": status, "pr_number": args.pr})
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
    except ReleaseClassificationBlocked as exc:
        mark_classification_blocked(api, args.pr, exc)
        _json_print(
            {
                "status": "classification-blocked",
                "pr_number": args.pr,
                "code": exc.code,
                "reason": str(exc),
            }
        )
        return 2
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
    except ReleaseClassificationBlocked as exc:
        mark_classification_blocked(api, args.pr, exc)
        _json_print(
            {
                "status": "classification-blocked",
                "pr_number": args.pr,
                "code": exc.code,
                "reason": str(exc),
            }
        )
        return 2
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
    except ReleaseClassificationBlocked as exc:
        mark_classification_blocked(api, args.pr, exc)
        _json_print(
            {
                "status": "classification-blocked",
                "pr_number": args.pr,
                "code": exc.code,
                "reason": str(exc),
            }
        )
        return 2
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
    select.add_argument(
        "--needs-resume-after-minutes",
        type=float,
        default=_needs_resume_default_minutes(),
    )
    select.set_defaults(handler=command_select)

    transition = subparsers.add_parser("transition")
    transition.add_argument("--pr", type=int, required=True)
    transition.add_argument("--state", choices=sorted(STATE_LABELS), required=True)
    transition.add_argument("--comment", default="")
    transition.set_defaults(handler=command_transition)
    halt = subparsers.add_parser("halt-merged")
    halt.add_argument("--pr", type=int, required=True)
    halt.add_argument("--merge-sha", required=True)
    halt.add_argument("--reason", required=True)
    halt.set_defaults(handler=command_halt)

    retry_blocked = subparsers.add_parser("retry-blocked")
    retry_blocked.add_argument("--pr", type=int, required=True)
    retry_blocked.add_argument("--expected-head-sha", required=True)
    retry_blocked.add_argument("--check-name", default="baseline")
    retry_blocked.set_defaults(handler=command_retry_blocked)

    enqueue_new = subparsers.add_parser("enqueue-loop-new")
    enqueue_new.add_argument("--pr", type=int, required=True)
    enqueue_new.add_argument("--expected-head-sha", required=True)
    enqueue_new.add_argument("--actor", required=True)
    enqueue_new.add_argument("--association", required=True)
    enqueue_new.add_argument("--check-name", default="baseline")
    enqueue_new.set_defaults(handler=command_enqueue_loop_new)

    enqueue_recovery = subparsers.add_parser("enqueue-loop-recovery")
    enqueue_recovery.add_argument("--pr", type=int, required=True)
    enqueue_recovery.add_argument("--expected-head-sha", required=True)
    enqueue_recovery.add_argument("--gate-pr", type=int, required=True)
    enqueue_recovery.add_argument("--expected-root", type=int, required=True)
    enqueue_recovery.add_argument("--actor", required=True)
    enqueue_recovery.add_argument("--association", required=True)
    enqueue_recovery.add_argument("--check-name", default="baseline")
    enqueue_recovery.set_defaults(handler=command_enqueue_loop_recovery)

    correct_identity = subparsers.add_parser("correct-loop-identity")
    correct_identity.add_argument("--pr", type=int, required=True)
    correct_identity.add_argument("--expected-head-sha", required=True)
    correct_identity.add_argument("--expected-old-root", type=int, required=True)
    correct_identity.add_argument("--actor", required=True)
    correct_identity.add_argument("--association", required=True)
    correct_identity.add_argument("--check-name", default="baseline")
    correct_identity.set_defaults(handler=command_correct_loop_identity)

    complete = subparsers.add_parser("complete-standard")
    complete.add_argument("--pr", type=int, required=True)
    complete.add_argument("--merge-sha", required=True)
    complete.add_argument(
        "--contour",
        choices=("repo-only", "production-verified"),
        required=True,
    )
    complete.set_defaults(handler=command_complete)

    resume_halted = subparsers.add_parser("resume-halted")
    resume_halted.add_argument("--pr", type=int, required=True)
    resume_halted.add_argument("--evidence-file", type=Path, required=True)
    resume_halted.set_defaults(handler=command_resume_halted)

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
