"""GitHub-native serialized merge/deploy queue for wb-core.

The queue keeps all durable state on pull requests through repository labels.
It never discovers work implicitly: only an open pull request carrying the
``release:ready`` label is eligible.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
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
    FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER,
    FINANCE_DEPLOY_LEASE_RECOVERY_PROOF_MARKER,
    FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
    HALTED_LABEL,
    HALT_PROOF_MARKER,
    IDENTITY_CORRECTION_PROOF_MARKER,
    NEEDS_RESUME_LABEL,
    NEW_ROOT_PROOF_MARKER,
    PRIMARY_STATE_LABELS,
    PRODUCTION_LABEL,
    PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
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
from packages.application.finance_migration_deploy_lease import (
    LEASE_POLICY as FINANCE_DEPLOY_LEASE_POLICY,
    LEASE_READBACK_CONTRACT as FINANCE_DEPLOY_LEASE_READBACK_CONTRACT,
    LEASE_RECOVERY_POLICY as FINANCE_DEPLOY_LEASE_RECOVERY_POLICY,
    baseline_invalidation_epoch as finance_deploy_lease_invalidation_epoch,
    evidence_fingerprint as finance_deploy_lease_evidence_fingerprint,
)

REPO_ONLY_LABEL = "scope:repo-only"
LIVE_RUNTIME_LABEL = "scope:live-runtime"
PRODUCTION_MUTATION_LABEL = "scope:production-mutation"

STANDARD_TASK_LABEL = "task:standard"
LOOP_TASK_LABEL = "task:loop"
LOOP_ROOT_PREFIX = "loop:root-"
LOOP_ACK_PREFIX = "loop:ack-"
LOOP_ACCEPT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
PRODUCTION_MUTATION_TERMINAL_ASSOCIATIONS = {"OWNER", "MEMBER"}
DEFAULT_NEEDS_RESUME_AFTER_SECONDS = 30 * 60
FINANCE_DEPLOY_LEASE_LABEL = "finance:migration-deploy-lease"
FINANCE_DEPLOY_LEASE_AUDIT_LABEL = "finance:migration-deploy-lease-audit"
FINANCE_DEPLOY_LEASE_RECOVERY_LABEL = "finance:migration-lease-recovery"
FINANCE_DEPLOY_LEASE_MIN_TTL_MINUTES = 30
FINANCE_DEPLOY_LEASE_MAX_TTL_MINUTES = 3 * 24 * 60

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
    FINANCE_DEPLOY_LEASE_LABEL: (
        "B60205",
        "Global fail-closed Finance migration deploy lease",
    ),
    FINANCE_DEPLOY_LEASE_AUDIT_LABEL: (
        "6F42C1",
        "Fail-closed Finance migration deploy-lease audit guard",
    ),
    FINANCE_DEPLOY_LEASE_RECOVERY_LABEL: (
        "F9D0C4",
        "Exact owner-bound recovery deploy under the Finance migration lease",
    ),
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


@dataclass(frozen=True)
class ProductionMutationTerminalizationCommand:
    pr: int
    head_sha: str
    merge_sha: str
    deployed_sha: str
    gate_comment_id: int
    gate_digest: str
    reconciliation_comment_id: int
    reconciliation_digest: str
    evidence_fingerprint: str


@dataclass(frozen=True)
class FinanceDeployLeaseCommand:
    operation: str
    anchor_pr: int
    task_id: str
    lease_id: str
    deployed_sha: str = ""
    head_sha: str = ""
    window_id: str = ""
    phase: str = ""
    ttl_minutes: int = 0
    revision: int = 0
    recovery_pr: int = 0
    recovery_head_sha: str = ""
    reconciliation_comment_id: int = 0
    reconciliation_digest: str = ""
    evidence_fingerprint: str = ""


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


def _exact_sha(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ReleaseBlocked(f"{field} must be an exact 40-character SHA")
    return normalized


def _sha256_fingerprint(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if (
        not normalized.startswith("sha256:")
        or len(normalized) != 71
        or any(character not in "0123456789abcdef" for character in normalized[7:])
    ):
        raise ReleaseBlocked(f"{field} must be an exact sha256 fingerprint")
    return normalized


def _comment_body_digest(comment: Mapping[str, Any]) -> str:
    body = str(comment.get("body") or "")
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _comment_by_id(
    api: ReleaseApi,
    number: int,
    comment_id: int,
    field: str,
) -> Mapping[str, Any]:
    for item in api.list_comments(number):
        if int(item.get("id") or 0) == comment_id:
            return item
    raise ReleaseBlocked(f"{field} does not identify a comment on PR #{number}")


def _comment_identity(
    comment: Mapping[str, Any],
    *,
    field: str,
) -> tuple[str, str]:
    author = comment.get("user")
    actor = str(author.get("login") or "") if isinstance(author, Mapping) else ""
    association = str(comment.get("author_association") or "").upper()
    if (
        not actor
        or actor in {"github-actions", "github-actions[bot]"}
        or association not in PRODUCTION_MUTATION_TERMINAL_ASSOCIATIONS
    ):
        raise ReleaseBlocked(
            f"{field} requires a non-bot OWNER or MEMBER GitHub identity"
        )
    return actor, association


def parse_production_mutation_terminalization_command(
    command: str,
) -> ProductionMutationTerminalizationCommand:
    parts = command.strip().split()
    if (
        len(parts) != 20
        or parts[:3] != ["/wb-core", "production-mutation", "complete"]
        or parts[4] != "head"
        or parts[6] != "merge"
        or parts[8] != "deployed"
        or parts[10] != "gate"
        or parts[12] != "gate-digest"
        or parts[14] != "reconciliation"
        or parts[16] != "reconciliation-digest"
        or parts[18] != "evidence"
    ):
        raise ReleaseBlocked(
            "production-mutation completion must bind PR, head, merge, deployed SHA, "
            "gate comment/digest, reconciliation comment/digest and evidence fingerprint"
        )
    try:
        number = int(parts[3])
        gate_comment_id = int(parts[11])
        reconciliation_comment_id = int(parts[15])
    except ValueError as exc:
        raise ReleaseBlocked(
            "production-mutation completion contains an invalid PR or comment identity"
        ) from exc
    if number <= 0 or gate_comment_id <= 0 or reconciliation_comment_id <= 0:
        raise ReleaseBlocked(
            "production-mutation completion requires positive PR and comment identities"
        )
    if gate_comment_id == reconciliation_comment_id:
        raise ReleaseBlocked(
            "human gate and reconciliation must be different evidence comments"
        )
    return ProductionMutationTerminalizationCommand(
        pr=number,
        head_sha=_exact_sha(parts[5], "head"),
        merge_sha=_exact_sha(parts[7], "merge"),
        deployed_sha=_exact_sha(parts[9], "deployed"),
        gate_comment_id=gate_comment_id,
        gate_digest=_sha256_fingerprint(parts[13], "gate-digest"),
        reconciliation_comment_id=reconciliation_comment_id,
        reconciliation_digest=_sha256_fingerprint(
            parts[17], "reconciliation-digest"
        ),
        evidence_fingerprint=_sha256_fingerprint(parts[19], "evidence"),
    )


def _lease_token(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in normalized
        )
    ):
        raise ReleaseBlocked(
            f"{field} must be a non-empty bounded token without whitespace"
        )
    return normalized


def parse_finance_deploy_lease_command(command: str) -> FinanceDeployLeaseCommand:
    parts = command.strip().split()
    if len(parts) < 4 or parts[:2] != ["/wb-core", "finance-lease"]:
        raise ReleaseBlocked("unsupported Finance deploy-lease command")
    operation = parts[2]
    try:
        anchor_pr = int(parts[3])
    except ValueError as exc:
        raise ReleaseBlocked(
            "Finance deploy-lease command requires a positive anchor PR"
        ) from exc
    if anchor_pr <= 0:
        raise ReleaseBlocked(
            "Finance deploy-lease command requires a positive anchor PR"
        )

    if operation == "acquire":
        if (
            len(parts) != 18
            or parts[4] != "head"
            or parts[6] != "deployed"
            or parts[8] != "task"
            or parts[10] != "lease"
            or parts[12] != "window"
            or parts[14] != "phase"
            or parts[16] != "ttl-minutes"
        ):
            raise ReleaseBlocked(
                "Finance deploy-lease acquire must bind anchor/head/deployed/task/"
                "lease/window/phase/ttl-minutes"
            )
        try:
            ttl_minutes = int(parts[17])
        except ValueError as exc:
            raise ReleaseBlocked(
                "Finance deploy-lease ttl-minutes must be an integer"
            ) from exc
        return FinanceDeployLeaseCommand(
            operation=operation,
            anchor_pr=anchor_pr,
            head_sha=_exact_sha(parts[5], "head"),
            deployed_sha=_exact_sha(parts[7], "deployed"),
            task_id=_lease_token(parts[9], "task"),
            lease_id=_lease_token(parts[11], "lease"),
            window_id=_lease_token(parts[13], "window"),
            phase=_lease_token(parts[15], "phase"),
            ttl_minutes=ttl_minutes,
        )

    if operation in {"rebind", "resume"}:
        expected_length = 20 if operation == "rebind" else 18
        if (
            len(parts) != expected_length
            or parts[4] != "deployed"
            or parts[6] != "task"
            or parts[8] != "lease"
            or parts[10] != "revision"
            or parts[12] != "window"
            or parts[14] != "phase"
        ):
            raise ReleaseBlocked(
                f"Finance deploy-lease {operation} must bind deployed/task/lease/"
                "revision/window/phase and bounded owner time"
            )
        if operation == "rebind":
            if parts[16] != "recovery-pr" or parts[18] != "ttl-minutes":
                raise ReleaseBlocked(
                    "Finance deploy-lease rebind requires recovery-pr and ttl-minutes"
                )
        elif parts[16] != "ttl-minutes":
            raise ReleaseBlocked(
                "Finance deploy-lease resume requires ttl-minutes"
            )
        try:
            revision = int(parts[11])
            recovery_pr = int(parts[17]) if operation == "rebind" else 0
            ttl_minutes = int(parts[19] if operation == "rebind" else parts[17])
        except ValueError as exc:
            raise ReleaseBlocked(
                f"Finance deploy-lease {operation} contains an invalid numeric identity"
            ) from exc
        return FinanceDeployLeaseCommand(
            operation=operation,
            anchor_pr=anchor_pr,
            deployed_sha=_exact_sha(parts[5], "deployed"),
            task_id=_lease_token(parts[7], "task"),
            lease_id=_lease_token(parts[9], "lease"),
            revision=revision,
            window_id=_lease_token(parts[13], "window"),
            phase=_lease_token(parts[15], "phase"),
            recovery_pr=recovery_pr,
            ttl_minutes=ttl_minutes,
        )

    if operation == "authorize-recovery":
        if (
            len(parts) != 14
            or parts[4] != "task"
            or parts[6] != "lease"
            or parts[8] != "revision"
            or parts[10] != "recovery-pr"
            or parts[12] != "head"
        ):
            raise ReleaseBlocked(
                "Finance deploy-lease recovery authorization must bind anchor/task/"
                "lease/revision/recovery-pr/head"
            )
        try:
            revision = int(parts[9])
            recovery_pr = int(parts[11])
        except ValueError as exc:
            raise ReleaseBlocked(
                "Finance deploy-lease recovery authorization contains an invalid identity"
            ) from exc
        return FinanceDeployLeaseCommand(
            operation=operation,
            anchor_pr=anchor_pr,
            task_id=_lease_token(parts[5], "task"),
            lease_id=_lease_token(parts[7], "lease"),
            revision=revision,
            recovery_pr=recovery_pr,
            recovery_head_sha=_exact_sha(parts[13], "recovery head"),
        )

    if operation in {"release", "abort"}:
        if (
            len(parts) != 18
            or parts[4] != "task"
            or parts[6] != "lease"
            or parts[8] != "revision"
            or parts[10] != "deployed"
            or parts[12] != "reconciliation"
            or parts[14] != "reconciliation-digest"
            or parts[16] != "evidence"
        ):
            raise ReleaseBlocked(
                f"Finance deploy-lease {operation} must bind task/lease/revision/"
                "deployed/reconciliation/digests"
            )
        try:
            revision = int(parts[9])
            reconciliation_comment_id = int(parts[13])
        except ValueError as exc:
            raise ReleaseBlocked(
                f"Finance deploy-lease {operation} contains an invalid identity"
            ) from exc
        return FinanceDeployLeaseCommand(
            operation=operation,
            anchor_pr=anchor_pr,
            task_id=_lease_token(parts[5], "task"),
            lease_id=_lease_token(parts[7], "lease"),
            revision=revision,
            deployed_sha=_exact_sha(parts[11], "deployed"),
            reconciliation_comment_id=reconciliation_comment_id,
            reconciliation_digest=_sha256_fingerprint(
                parts[15], "reconciliation-digest"
            ),
            evidence_fingerprint=_sha256_fingerprint(parts[17], "evidence"),
        )
    raise ReleaseBlocked(
        "Finance deploy-lease operation must be acquire, authorize-recovery, "
        "rebind, resume, release, or abort"
    )


def _has_comment_proof(api: ReleaseApi, number: int, marker: str, **values: object) -> bool:
    expected = _proof_marker(marker, **values)
    for item in api.list_comments(number):
        if expected not in str(item.get("body") or ""):
            continue
        author = item.get("user")
        if not isinstance(author, Mapping):
            continue
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
        if not isinstance(author, Mapping) or str(author.get("login") or "") not in {
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


def _finance_deploy_lease_items(api: ReleaseApi) -> list[dict[str, Any]]:
    items: dict[int, dict[str, Any]] = {}
    for item in api.list_issues_by_label(
        FINANCE_DEPLOY_LEASE_LABEL,
        state="all",
    ):
        if "pull_request" in item:
            items[int(item.get("number") or 0)] = item
    for item in api.list_issues_by_label(
        FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
        state="all",
    ):
        if "pull_request" not in item:
            continue
        number = int(item.get("number") or 0)
        if number in items:
            continue
        terminal = _repo_owned_marker_fields(
            api,
            number,
            FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
        )
        if not terminal:
            items[number] = item
    return [items[number] for number in sorted(items)]


def _finance_deploy_lease_binding_fields(
    api: ReleaseApi,
    anchor_pr: int,
) -> tuple[list[dict[str, str]], list[str]]:
    bindings: dict[int, dict[str, str]] = {}
    ambiguous: list[str] = []
    for fields in _repo_owned_marker_fields(
        api,
        anchor_pr,
        FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER,
    ):
        try:
            revision = int(fields.get("revision") or 0)
            proof_anchor = int(fields.get("anchor") or 0)
            acquired = int(fields.get("acquired") or 0)
            expires = int(fields.get("expires") or 0)
            ttl = int(fields.get("ttl") or 0)
        except ValueError:
            ambiguous.append("binding_numeric_identity_invalid")
            continue
        if (
            proof_anchor != anchor_pr
            or revision <= 0
            or acquired <= 0
            or expires <= acquired
            or ttl < FINANCE_DEPLOY_LEASE_MIN_TTL_MINUTES
            or ttl > FINANCE_DEPLOY_LEASE_MAX_TTL_MINUTES
            or expires != acquired + ttl * 60
        ):
            ambiguous.append("binding_boundary_invalid")
            continue
        required = (
            "actor",
            "association",
            "deployed",
            "head",
            "lease",
            "operation",
            "phase",
            "task",
            "window",
        )
        if any(not str(fields.get(key) or "") for key in required):
            ambiguous.append("binding_identity_incomplete")
            continue
        try:
            _exact_sha(fields["deployed"], "lease deployed")
            _exact_sha(fields["head"], "lease head")
            for key in ("lease", "phase", "task", "window"):
                _lease_token(fields[key], key)
        except ReleaseBlocked:
            ambiguous.append("binding_identity_invalid")
            continue
        previous = bindings.get(revision)
        if previous is not None and previous != fields:
            ambiguous.append(f"binding_revision_{revision}_conflict")
            continue
        bindings[revision] = dict(fields)
    ordered = [bindings[key] for key in sorted(bindings)]
    revisions = [int(item["revision"]) for item in ordered]
    if revisions and revisions != list(range(1, max(revisions) + 1)):
        ambiguous.append("binding_revision_gap")
    if ordered and ordered[0].get("operation") != "acquire":
        ambiguous.append("binding_revision_one_not_acquire")
    for item in ordered[1:]:
        if item.get("operation") not in {"rebind", "resume"}:
            ambiguous.append("binding_followup_operation_invalid")
    if ordered:
        identity = (ordered[0]["task"], ordered[0]["lease"], ordered[0]["head"])
        for item in ordered[1:]:
            if (item["task"], item["lease"], item["head"]) != identity:
                ambiguous.append("binding_owner_identity_changed")
    return ordered, sorted(set(ambiguous))


def finance_deploy_lease_state(
    api: ReleaseApi,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Read the single durable global Finance lease without mutating GitHub."""

    observed = (
        datetime.now(timezone.utc).timestamp()
        if now is None
        else float(now)
    )
    items = _finance_deploy_lease_items(api)
    if not items:
        return {
            "status": "absent",
            "global_release_blocked": False,
            "allows_finance_migration": False,
            "ambiguous_reasons": [],
        }
    if len(items) != 1:
        return {
            "status": "ambiguous",
            "global_release_blocked": True,
            "allows_finance_migration": False,
            "ambiguous_reasons": ["multiple_active_lease_labels"],
            "anchor_prs": sorted(int(item.get("number") or 0) for item in items),
        }
    anchor_pr = int(items[0].get("number") or 0)
    pull = api.get_pull(anchor_pr)
    labels = label_names(pull)
    hold_label_present = FINANCE_DEPLOY_LEASE_LABEL in labels
    audit_label_present = FINANCE_DEPLOY_LEASE_AUDIT_LABEL in labels
    bindings, ambiguous = _finance_deploy_lease_binding_fields(api, anchor_pr)
    terminal = _repo_owned_marker_fields(
        api,
        anchor_pr,
        FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
    )
    if terminal:
        if hold_label_present:
            ambiguous.append("terminal_proof_present_while_lease_label_active")
        elif audit_label_present:
            return {
                "status": "absent",
                "global_release_blocked": False,
                "allows_finance_migration": False,
                "ambiguous_reasons": [],
                "terminal_anchor_pr": anchor_pr,
            }
    if not hold_label_present:
        ambiguous.append("active_lease_label_lost")
    if not audit_label_present:
        ambiguous.append("lease_audit_anchor_lost")
    if not terminal_state_proven(api, pull):
        ambiguous.append("anchor_terminal_release_not_proven")
    if not bindings:
        ambiguous.append("binding_proof_missing")
    if ambiguous or not bindings:
        return {
            "status": "ambiguous",
            "global_release_blocked": True,
            "allows_finance_migration": False,
            "ambiguous_reasons": sorted(set(ambiguous)),
            "anchor_prs": [anchor_pr],
        }

    current = bindings[-1]
    expires = int(current["expires"])
    status = "active" if expires > observed else "stale"
    recovery_items = [
        item
        for item in api.list_issues_by_label(
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            state="all",
        )
        if "pull_request" in item
    ]
    recovery_pending = False
    if recovery_items:
        if len(recovery_items) != 1:
            ambiguous.append("multiple_recovery_authorizations")
        else:
            recovery_pr = int(recovery_items[0].get("number") or 0)
            recovery_pull = api.get_pull(recovery_pr)
            recovery_head = str(
                (recovery_pull.get("head") or {}).get("sha") or ""
            ).lower()
            authorized = _finance_recovery_authorization_matches(
                api,
                recovery_pr=recovery_pr,
                recovery_head=recovery_head,
                lease={
                    "anchor_pr": anchor_pr,
                    "head_sha": current["head"],
                    "lease_id": current["lease"],
                    "revision": int(current["revision"]),
                    "task_id": current["task"],
                },
            )
            cleanup_pending = (
                current.get("operation") == "rebind"
                and int(current.get("recovery_pr") or 0) == recovery_pr
                and bool(recovery_pull.get("merged"))
            )
            if authorized or cleanup_pending:
                recovery_pending = True
            else:
                ambiguous.append("recovery_authorization_identity_invalid")
    if ambiguous:
        return {
            "status": "ambiguous",
            "global_release_blocked": True,
            "allows_finance_migration": False,
            "ambiguous_reasons": sorted(set(ambiguous)),
            "anchor_prs": [anchor_pr],
        }
    acquired_iso = datetime.fromtimestamp(
        int(current["acquired"]),
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    expires_iso = datetime.fromtimestamp(
        expires,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    observed_iso = datetime.fromtimestamp(
        observed,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    invalidation_epoch = finance_deploy_lease_invalidation_epoch(
        anchor_pr=anchor_pr,
        deployed_sha=current["deployed"],
        lease_id=current["lease"],
        revision=int(current["revision"]),
        task_id=current["task"],
    )
    payload: dict[str, Any] = {
        "contract_version": FINANCE_DEPLOY_LEASE_READBACK_CONTRACT,
        "policy": FINANCE_DEPLOY_LEASE_POLICY,
        "status": status,
        "allows_finance_migration": status == "active" and not recovery_pending,
        "global_release_blocked": True,
        "observed_at": observed_iso,
        "ambiguous_reasons": [],
        "lease": {
            "lease_id": current["lease"],
            "task_id": current["task"],
            "anchor_pr": anchor_pr,
            "head_sha": current["head"],
            "deployed_sha": current["deployed"],
            "window_id": current["window"],
            "phase": current["phase"],
            "revision": int(current["revision"]),
            "acquired_at": acquired_iso,
            "expires_at": expires_iso,
            "baseline_invalidation_epoch": invalidation_epoch,
            "recovery_policy": FINANCE_DEPLOY_LEASE_RECOVERY_POLICY,
        },
    }
    if recovery_pending:
        payload["recovery_pending"] = True
    payload["fingerprint"] = finance_deploy_lease_evidence_fingerprint(payload)
    return payload


def _finance_deploy_lease_matches_command(
    state: Mapping[str, Any],
    command: FinanceDeployLeaseCommand,
    *,
    ignore_revision: bool = False,
) -> Mapping[str, Any]:
    lease = state.get("lease")
    if not isinstance(lease, Mapping):
        raise ReleaseBlocked("active Finance deploy lease identity is unavailable")
    expected = {
        "anchor_pr": command.anchor_pr,
        "task_id": command.task_id,
        "lease_id": command.lease_id,
    }
    mismatches = [
        key for key, value in expected.items() if lease.get(key) != value
    ]
    if (
        command.revision
        and not ignore_revision
        and int(lease.get("revision") or 0) != command.revision
    ):
        mismatches.append("revision")
    if mismatches:
        raise ReleaseBlocked(
            "Finance deploy-lease command does not match the active identity: "
            + ", ".join(sorted(set(mismatches)))
        )
    return lease


def _finance_deploy_lease_blocking_releases(
    api: ReleaseApi,
) -> list[tuple[int, str]]:
    blocked: dict[int, str] = {}
    for label in (
        RUNNING_LABEL,
        AWAITING_AGENT_LABEL,
        AWAITING_UI_LABEL,
        HALTED_LABEL,
    ):
        for item in api.list_issues_by_label(label, state="all"):
            if "pull_request" in item:
                blocked[int(item.get("number") or 0)] = label
    return sorted(blocked.items())


def _validate_finance_deploy_lease_actor(
    *,
    actor: str,
    association: str,
) -> str:
    normalized = str(association or "").upper()
    if normalized not in PRODUCTION_MUTATION_TERMINAL_ASSOCIATIONS:
        raise ReleaseBlocked(
            "Finance deploy-lease command requires OWNER or MEMBER association"
        )
    if not actor or actor in {"github-actions", "github-actions[bot]"}:
        raise ReleaseBlocked(
            "Finance deploy-lease command requires a non-bot owner identity"
        )
    return normalized


def _validate_canonical_deploy_evidence(
    deploy_evidence: Mapping[str, Any] | None,
    *,
    pr: int,
    head: str,
    deployed: str,
) -> str:
    if not isinstance(deploy_evidence, Mapping):
        raise ReleaseBlocked(
            "Finance deploy-lease transition requires canonical deployment readback"
        )
    required = {
        "healthy": True,
        "status": "reconciled",
        "pr": pr,
        "head": head,
        "merge": deployed,
        "expected_sha": deployed,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "read_only": True,
        "repairs_applied": False,
    }
    mismatches = [
        key for key, value in required.items() if deploy_evidence.get(key) != value
    ]
    if mismatches:
        raise ReleaseBlocked(
            "Finance deploy-lease production SHA readback is not exact: "
            + ", ".join(mismatches)
        )
    return "sha256:" + hashlib.sha256(
        json.dumps(
            deploy_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _finance_deploy_lease_anchor_preflight(
    api: ReleaseApi,
    command: FinanceDeployLeaseCommand,
) -> tuple[dict[str, Any], str]:
    pull = api.get_pull(command.anchor_pr)
    labels = label_names(pull)
    if (
        task_class_from_labels(labels) != STANDARD_TASK_LABEL
        or scope_from_labels(labels) != LIVE_RUNTIME_LABEL
    ):
        raise ReleaseBlocked(
            "Finance deploy-lease anchor must be a STANDARD live-runtime release"
        )
    if not bool(pull.get("merged")) or not terminal_state_proven(api, pull):
        raise ReleaseBlocked(
            "Finance deploy-lease anchor must be a proven terminal production release"
        )
    actual_head = str((pull.get("head") or {}).get("sha") or "").lower()
    merge_sha = str(pull.get("merge_commit_sha") or "").lower()
    if command.head_sha and actual_head != command.head_sha:
        raise ReleaseBlocked("Finance deploy-lease anchor head SHA is stale")
    comparison = api.compare(merge_sha, command.deployed_sha)
    if (
        str(comparison.get("status") or "") not in {"ahead", "identical"}
        or int(comparison.get("behind_by") or 0) != 0
    ):
        raise ReleaseBlocked(
            "Finance deploy-lease deployed SHA must be the anchor merge or a descendant"
        )
    return pull, merge_sha


def acquire_finance_deploy_lease(
    api: ReleaseApi,
    command: FinanceDeployLeaseCommand,
    deploy_evidence: Mapping[str, Any] | None,
    *,
    actor: str,
    association: str,
    actions_owned: bool,
    now: float | None = None,
) -> str:
    if not actions_owned:
        raise ReleaseBlocked(
            "Finance deploy-lease acquire is restricted to trusted-main GitHub Actions"
        )
    normalized_association = _validate_finance_deploy_lease_actor(
        actor=actor,
        association=association,
    )
    if command.operation != "acquire":
        raise ReleaseBlocked("Finance deploy-lease acquire command is required")
    if not (
        FINANCE_DEPLOY_LEASE_MIN_TTL_MINUTES
        <= command.ttl_minutes
        <= FINANCE_DEPLOY_LEASE_MAX_TTL_MINUTES
    ):
        raise ReleaseBlocked(
            "Finance deploy-lease ttl-minutes is outside the bounded policy"
        )
    _finance_deploy_lease_anchor_preflight(api, command)
    deploy_digest = _validate_canonical_deploy_evidence(
        deploy_evidence,
        pr=command.anchor_pr,
        head=command.head_sha,
        deployed=command.deployed_sha,
    )
    blocked = _finance_deploy_lease_blocking_releases(api)
    if blocked:
        raise ReleaseBlocked(
            "Finance deploy-lease acquire requires no running/awaiting/halted "
            "release: "
            + ", ".join(f"#{number}:{label}" for number, label in blocked)
        )
    recovery_items = [
        item
        for item in api.list_issues_by_label(
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            state="all",
        )
        if "pull_request" in item
    ]
    if recovery_items:
        raise ReleaseBlocked(
            "Finance deploy-lease acquire found an existing recovery authorization"
        )

    items = _finance_deploy_lease_items(api)
    if len(items) > 1 or (
        items and int(items[0].get("number") or 0) != command.anchor_pr
    ):
        raise ReleaseBlocked(
            "another or ambiguous global Finance deploy lease already exists"
        )
    bindings, ambiguous = _finance_deploy_lease_binding_fields(
        api,
        command.anchor_pr,
    )
    terminal = _repo_owned_marker_fields(
        api,
        command.anchor_pr,
        FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
    )
    if terminal:
        raise ReleaseBlocked(
            "Finance deploy-lease anchor already has terminal lease proof"
        )
    if ambiguous:
        raise ReleaseBlocked(
            "Finance deploy-lease binding history is ambiguous: "
            + ", ".join(ambiguous)
        )
    if bindings:
        first = bindings[0]
        expected = {
            "deployed": command.deployed_sha,
            "head": command.head_sha,
            "lease": command.lease_id,
            "operation": "acquire",
            "phase": command.phase,
            "task": command.task_id,
            "window": command.window_id,
            "ttl": str(command.ttl_minutes),
        }
        if int(first["revision"]) != 1 or any(
            first.get(key) != value for key, value in expected.items()
        ):
            raise ReleaseBlocked(
                "existing Finance deploy-lease binding does not match this acquire"
            )
        current_labels = label_names(api.get_pull(command.anchor_pr))
        if (
            FINANCE_DEPLOY_LEASE_LABEL not in current_labels
            or FINANCE_DEPLOY_LEASE_AUDIT_LABEL not in current_labels
        ):
            api.add_labels(
                command.anchor_pr,
                [
                    FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
                    FINANCE_DEPLOY_LEASE_LABEL,
                ],
            )
        return "already-acquired"

    acquired = int(
        datetime.now(timezone.utc).timestamp()
        if now is None
        else float(now)
    )
    values = {
        "acquired": acquired,
        "actor": actor,
        "anchor": command.anchor_pr,
        "association": normalized_association,
        "deploy_evidence": deploy_digest,
        "deployed": command.deployed_sha,
        "expires": acquired + command.ttl_minutes * 60,
        "head": command.head_sha,
        "lease": command.lease_id,
        "operation": "acquire",
        "phase": command.phase,
        "revision": 1,
        "task": command.task_id,
        "ttl": command.ttl_minutes,
        "window": command.window_id,
    }
    # Durable audit + global hold labels are written first: a disconnect before
    # the proof comment leaves an ambiguous label-owned state that blocks every
    # release. The same exact acquire command can then heal it idempotently.
    api.add_labels(
        command.anchor_pr,
        [
            FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
            FINANCE_DEPLOY_LEASE_LABEL,
        ],
    )
    api.add_comment(
        command.anchor_pr,
        "Release Train acquired the global fail-closed Finance migration "
        f"deploy lease `{command.lease_id}` for task `{command.task_id}` at "
        f"deployed SHA `{command.deployed_sha}`.\n\n"
        + _proof_marker(
            FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER,
            **values,
        ),
    )
    return "acquired"


def _finance_recovery_authorization_matches(
    api: ReleaseApi,
    *,
    recovery_pr: int,
    recovery_head: str,
    lease: Mapping[str, Any],
) -> bool:
    expected = {
        "anchor": str(int(lease["anchor_pr"])),
        "head": recovery_head,
        "lease": str(lease["lease_id"]),
        "recovery_pr": str(recovery_pr),
        "revision": str(int(lease["revision"])),
        "task": str(lease["task_id"]),
    }
    for fields in _repo_owned_marker_fields(
        api,
        recovery_pr,
        FINANCE_DEPLOY_LEASE_RECOVERY_PROOF_MARKER,
    ):
        if all(fields.get(key) == value for key, value in expected.items()):
            return True
    return False


def authorize_finance_deploy_lease_recovery(
    api: ReleaseApi,
    command: FinanceDeployLeaseCommand,
    *,
    actor: str,
    association: str,
    actions_owned: bool,
) -> str:
    if not actions_owned:
        raise ReleaseBlocked(
            "Finance recovery authorization is restricted to trusted-main GitHub Actions"
        )
    normalized_association = _validate_finance_deploy_lease_actor(
        actor=actor,
        association=association,
    )
    state = finance_deploy_lease_state(api)
    if state.get("status") not in {"active", "stale"}:
        raise ReleaseBlocked(
            "Finance recovery authorization requires one unambiguous active/stale lease"
        )
    lease = _finance_deploy_lease_matches_command(state, command)
    if command.recovery_pr <= 0:
        raise ReleaseBlocked("Finance recovery PR must be positive")
    recovery = api.get_pull(command.recovery_pr)
    labels = label_names(recovery)
    if (
        str(recovery.get("state") or "").lower() != "open"
        or bool(recovery.get("draft"))
        or task_class_from_labels(labels) != STANDARD_TASK_LABEL
        or scope_from_labels(labels) != LIVE_RUNTIME_LABEL
    ):
        raise ReleaseBlocked(
            "Finance recovery must be an open non-draft STANDARD live-runtime PR"
        )
    actual_head = str((recovery.get("head") or {}).get("sha") or "").lower()
    if actual_head != command.recovery_head_sha:
        raise ReleaseBlocked("Finance recovery PR head SHA is stale")
    if not _has_successful_check(api, actual_head, "baseline"):
        raise ReleaseBlocked(
            "Finance recovery authorization requires successful exact-head baseline"
        )
    existing = [
        item
        for item in api.list_issues_by_label(
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            state="all",
        )
        if "pull_request" in item
        and int(item.get("number") or 0) != command.recovery_pr
    ]
    if existing:
        raise ReleaseBlocked(
            "another Finance owner-bound recovery authorization is still active"
        )
    if _finance_recovery_authorization_matches(
        api,
        recovery_pr=command.recovery_pr,
        recovery_head=actual_head,
        lease=lease,
    ):
        api.add_labels(
            command.recovery_pr,
            [FINANCE_DEPLOY_LEASE_RECOVERY_LABEL],
        )
        return "already-authorized"
    values = {
        "actor": actor,
        "anchor": command.anchor_pr,
        "association": normalized_association,
        "head": actual_head,
        "lease": command.lease_id,
        "recovery_pr": command.recovery_pr,
        "revision": command.revision,
        "task": command.task_id,
    }
    api.add_comment(
        command.recovery_pr,
        "Release Train authorized only this exact owner-bound recovery deploy "
        f"under Finance lease `{command.lease_id}`. A successful deploy must "
        "be rebound before any migration phase can continue.\n\n"
        + _proof_marker(
            FINANCE_DEPLOY_LEASE_RECOVERY_PROOF_MARKER,
            **values,
        ),
    )
    api.add_labels(
        command.recovery_pr,
        [FINANCE_DEPLOY_LEASE_RECOVERY_LABEL],
    )
    return "authorized"


def rebind_finance_deploy_lease(
    api: ReleaseApi,
    command: FinanceDeployLeaseCommand,
    deploy_evidence: Mapping[str, Any] | None,
    *,
    actor: str,
    association: str,
    actions_owned: bool,
    now: float | None = None,
) -> str:
    if not actions_owned:
        raise ReleaseBlocked(
            "Finance deploy-lease rebind/resume is restricted to trusted-main Actions"
        )
    normalized_association = _validate_finance_deploy_lease_actor(
        actor=actor,
        association=association,
    )
    if command.operation not in {"rebind", "resume"}:
        raise ReleaseBlocked("Finance deploy-lease rebind/resume command is required")
    if not (
        FINANCE_DEPLOY_LEASE_MIN_TTL_MINUTES
        <= command.ttl_minutes
        <= FINANCE_DEPLOY_LEASE_MAX_TTL_MINUTES
    ):
        raise ReleaseBlocked(
            "Finance deploy-lease ttl-minutes is outside the bounded policy"
        )
    state = finance_deploy_lease_state(api)
    if state.get("status") not in {"active", "stale"}:
        raise ReleaseBlocked(
            "Finance deploy-lease rebind requires one unambiguous active/stale lease"
        )
    lease = _finance_deploy_lease_matches_command(
        state,
        command,
        ignore_revision=True,
    )
    current_revision = int(lease.get("revision") or 0)
    if current_revision == command.revision + 1:
        if (
            str(lease.get("deployed_sha") or "") != command.deployed_sha
            or str(lease.get("window_id") or "") != command.window_id
            or str(lease.get("phase") or "") != command.phase
        ):
            raise ReleaseBlocked(
                "current Finance deploy-lease revision conflicts with repeated rebind"
            )
        bindings, ambiguous = _finance_deploy_lease_binding_fields(
            api,
            command.anchor_pr,
        )
        matching = next(
            (
                item
                for item in bindings
                if int(item.get("revision") or 0) == current_revision
            ),
            None,
        )
        if (
            ambiguous
            or matching is None
            or matching.get("operation") != command.operation
        ):
            raise ReleaseBlocked(
                "repeated Finance deploy-lease rebind proof is ambiguous"
            )
        repeated_blocked = _finance_deploy_lease_blocking_releases(api)
        if repeated_blocked:
            raise ReleaseBlocked(
                "repeated Finance deploy-lease rebind requires no active deploy"
            )
        anchor = api.get_pull(command.anchor_pr)
        anchor_merge = str(anchor.get("merge_commit_sha") or "").lower()
        comparison = api.compare(anchor_merge, command.deployed_sha)
        if (
            str(comparison.get("status") or "") not in {"ahead", "identical"}
            or int(comparison.get("behind_by") or 0) != 0
        ):
            raise ReleaseBlocked(
                "repeated Finance deploy-lease rebind SHA is not an anchor descendant"
            )
        _validate_canonical_deploy_evidence(
            deploy_evidence,
            pr=command.anchor_pr,
            head=str(lease.get("head_sha") or ""),
            deployed=command.deployed_sha,
        )
        if command.recovery_pr:
            api.remove_label(
                command.recovery_pr,
                FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            )
        return "already-rebound"
    if current_revision != command.revision:
        raise ReleaseBlocked("Finance deploy-lease rebind revision is stale")
    blocked = _finance_deploy_lease_blocking_releases(api)
    if blocked:
        raise ReleaseBlocked(
            "Finance deploy-lease rebind requires no running/awaiting/halted release: "
            + ", ".join(f"#{number}:{label}" for number, label in blocked)
        )
    head = str(lease.get("head_sha") or "")
    anchor = api.get_pull(command.anchor_pr)
    anchor_merge = str(anchor.get("merge_commit_sha") or "").lower()
    comparison = api.compare(anchor_merge, command.deployed_sha)
    if (
        str(comparison.get("status") or "") not in {"ahead", "identical"}
        or int(comparison.get("behind_by") or 0) != 0
    ):
        raise ReleaseBlocked(
            "Finance deploy-lease rebind SHA is not an anchor descendant"
        )
    deploy_digest = _validate_canonical_deploy_evidence(
        deploy_evidence,
        pr=command.anchor_pr,
        head=head,
        deployed=command.deployed_sha,
    )
    if command.operation == "resume":
        if command.deployed_sha != lease.get("deployed_sha"):
            raise ReleaseBlocked(
                "Finance deploy-lease resume cannot change the deployed SHA"
            )
    else:
        if command.recovery_pr <= 0:
            raise ReleaseBlocked("Finance deploy-lease rebind requires recovery PR")
        recovery = api.get_pull(command.recovery_pr)
        recovery_head = str((recovery.get("head") or {}).get("sha") or "").lower()
        if not _finance_recovery_authorization_matches(
            api,
            recovery_pr=command.recovery_pr,
            recovery_head=recovery_head,
            lease=lease,
        ):
            raise ReleaseBlocked(
                "Finance deploy-lease rebind lacks exact owner-bound recovery proof"
            )
        if not bool(recovery.get("merged")) or not terminal_state_proven(api, recovery):
            raise ReleaseBlocked(
                "Finance deploy-lease rebind requires proven terminal recovery deploy"
            )
        if str(recovery.get("merge_commit_sha") or "").lower() != command.deployed_sha:
            raise ReleaseBlocked(
                "Finance deploy-lease rebind deployed SHA is not the recovery merge"
            )

    bindings, ambiguous = _finance_deploy_lease_binding_fields(
        api,
        command.anchor_pr,
    )
    if ambiguous or not bindings:
        raise ReleaseBlocked(
            "Finance deploy-lease binding history is missing or ambiguous"
        )
    next_revision = command.revision + 1
    existing = next(
        (
            item
            for item in bindings
            if int(item.get("revision") or 0) == next_revision
        ),
        None,
    )
    expected = {
        "deployed": command.deployed_sha,
        "head": head,
        "lease": command.lease_id,
        "operation": command.operation,
        "phase": command.phase,
        "task": command.task_id,
        "window": command.window_id,
        "ttl": str(command.ttl_minutes),
    }
    if existing is not None:
        if any(existing.get(key) != value for key, value in expected.items()):
            raise ReleaseBlocked(
                "existing Finance deploy-lease revision conflicts with rebind"
            )
        if command.recovery_pr:
            api.remove_label(
                command.recovery_pr,
                FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            )
        return "already-rebound"

    acquired = int(
        datetime.now(timezone.utc).timestamp()
        if now is None
        else float(now)
    )
    values = {
        "acquired": acquired,
        "actor": actor,
        "anchor": command.anchor_pr,
        "association": normalized_association,
        "deploy_evidence": deploy_digest,
        "deployed": command.deployed_sha,
        "expires": acquired + command.ttl_minutes * 60,
        "head": head,
        "lease": command.lease_id,
        "operation": command.operation,
        "phase": command.phase,
        "recovery_pr": command.recovery_pr,
        "revision": next_revision,
        "task": command.task_id,
        "ttl": command.ttl_minutes,
        "window": command.window_id,
    }
    api.add_comment(
        command.anchor_pr,
        "Release Train rebound the global Finance deploy lease to revision "
        f"`{next_revision}` and deployed SHA `{command.deployed_sha}`. Every "
        "earlier baseline, snapshot plan and fingerprint is invalid.\n\n"
        + _proof_marker(
            FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER,
            **values,
        ),
    )
    if command.recovery_pr:
        api.remove_label(
            command.recovery_pr,
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
        )
    return "rebound"


def terminalize_finance_deploy_lease(
    api: ReleaseApi,
    command: FinanceDeployLeaseCommand,
    deploy_evidence: Mapping[str, Any] | None,
    *,
    actor: str,
    association: str,
    actions_owned: bool,
) -> str:
    if not actions_owned:
        raise ReleaseBlocked(
            "Finance deploy-lease release/abort is restricted to trusted-main Actions"
        )
    normalized_association = _validate_finance_deploy_lease_actor(
        actor=actor,
        association=association,
    )
    terminal_expected = {
        "deployed": command.deployed_sha,
        "evidence": command.evidence_fingerprint,
        "lease": command.lease_id,
        "mode": command.operation,
        "reconciliation_digest": command.reconciliation_digest,
        "reconciliation_id": str(command.reconciliation_comment_id),
        "revision": str(command.revision),
        "task": command.task_id,
    }
    terminal_proven = any(
        all(fields.get(key) == value for key, value in terminal_expected.items())
        for fields in _repo_owned_marker_fields(
            api,
            command.anchor_pr,
            FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
        )
    )
    if terminal_proven:
        pull = api.get_pull(command.anchor_pr)
        _validate_canonical_deploy_evidence(
            deploy_evidence,
            pr=command.anchor_pr,
            head=str((pull.get("head") or {}).get("sha") or "").lower(),
            deployed=command.deployed_sha,
        )
        api.remove_label(command.anchor_pr, FINANCE_DEPLOY_LEASE_LABEL)
        api.remove_label(
            command.anchor_pr,
            FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
        )
        for item in api.list_issues_by_label(
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            state="all",
        ):
            if "pull_request" in item:
                api.remove_label(
                    int(item.get("number") or 0),
                    FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
                )
        return "already-terminal"
    state = finance_deploy_lease_state(api)
    if state.get("status") == "absent":
        raise ReleaseBlocked("Finance deploy lease is absent without terminal proof")
    if state.get("status") not in {"active", "stale"}:
        raise ReleaseBlocked(
            "Finance deploy-lease release/abort requires one unambiguous lease"
        )
    lease = _finance_deploy_lease_matches_command(state, command)
    if command.deployed_sha != lease.get("deployed_sha"):
        raise ReleaseBlocked(
            "Finance deploy-lease terminal command deployed SHA is stale"
        )
    blocked = _finance_deploy_lease_blocking_releases(api)
    if blocked:
        raise ReleaseBlocked(
            "Finance deploy-lease terminal transition requires no active deploy: "
            + ", ".join(f"#{number}:{label}" for number, label in blocked)
        )
    deploy_digest = _validate_canonical_deploy_evidence(
        deploy_evidence,
        pr=command.anchor_pr,
        head=str(lease.get("head_sha") or ""),
        deployed=command.deployed_sha,
    )
    reconciliation = _comment_by_id(
        api,
        command.anchor_pr,
        command.reconciliation_comment_id,
        "Finance lease reconciliation",
    )
    reconciliation_actor, reconciliation_association = _comment_identity(
        reconciliation,
        field="Finance lease reconciliation",
    )
    if _comment_body_digest(reconciliation) != command.reconciliation_digest:
        raise ReleaseBlocked("Finance lease reconciliation comment digest is stale")
    body = str(reconciliation.get("body") or "")
    folded = body.casefold()
    required_common = (
        f"task={command.task_id}".casefold(),
        f"lease={command.lease_id}".casefold(),
        f"revision={command.revision}".casefold(),
        f"deployed={command.deployed_sha}".casefold(),
        f"evidence={command.evidence_fingerprint}".casefold(),
        "manual_barrier=released",
        "writers=restored",
        "timers=restored",
        "policy=restored",
        "non_target=unchanged",
        "sha_readback=exact",
    )
    required_mode = (
        ("migration_abort=complete", "canonical_source=monolith")
        if command.operation == "abort"
        else (
            "post_cutover_reconciliation=complete",
            "canonical_source=split",
        )
    )
    missing = [
        item for item in (*required_common, *required_mode) if item not in folded
    ]
    if missing:
        raise ReleaseBlocked(
            "Finance lease reconciliation lacks exact restore/non-target/mode "
            "readback: "
            + ", ".join(missing)
        )
    values = {
        "actor": actor,
        "anchor": command.anchor_pr,
        "association": normalized_association,
        "deploy_evidence": deploy_digest,
        "deployed": command.deployed_sha,
        "evidence": command.evidence_fingerprint,
        "lease": command.lease_id,
        "mode": command.operation,
        "reconciliation_actor": reconciliation_actor,
        "reconciliation_association": reconciliation_association,
        "reconciliation_digest": command.reconciliation_digest,
        "reconciliation_id": command.reconciliation_comment_id,
        "revision": command.revision,
        "task": command.task_id,
    }
    if not _has_comment_proof(
        api,
        command.anchor_pr,
        FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
        **values,
    ):
        api.add_comment(
            command.anchor_pr,
            "Release Train terminalized the global Finance deploy lease only "
            "after exact migration reconciliation and full control/non-target "
            "readback.\n\n"
            + _proof_marker(
                FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER,
                **values,
            ),
        )
    api.remove_label(command.anchor_pr, FINANCE_DEPLOY_LEASE_LABEL)
    api.remove_label(
        command.anchor_pr,
        FINANCE_DEPLOY_LEASE_AUDIT_LABEL,
    )
    for item in api.list_issues_by_label(
        FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
        state="all",
    ):
        if "pull_request" in item:
            api.remove_label(
                int(item.get("number") or 0),
                FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            )
    api.dispatch_workflow("release-train.yml", "main")
    return "released" if command.operation == "release" else "aborted"


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
    lease_state = finance_deploy_lease_state(api)
    if lease_state.get("global_release_blocked") is True:
        lease = lease_state.get("lease") or {}
        return {
            "status": "finance-deploy-lease",
            "pr_number": int(lease.get("anchor_pr") or 0),
            "lease_status": str(lease_state.get("status") or "ambiguous"),
            "lease_id": str(lease.get("lease_id") or ""),
            "revision": int(lease.get("revision") or 0),
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
    lease_state = finance_deploy_lease_state(api)
    if lease_state.get("global_release_blocked") is True:
        if lease_state.get("status") != "active":
            raise ReleaseBlocked(
                "global Finance migration deploy lease is stale or ambiguous"
            )
        lease = lease_state.get("lease")
        if not isinstance(lease, Mapping):
            raise ReleaseBlocked(
                "global Finance migration deploy lease identity is unavailable"
            )
        pull = api.get_pull(number)
        head = str((pull.get("head") or {}).get("sha") or "").lower()
        if not _finance_recovery_authorization_matches(
            api,
            recovery_pr=number,
            recovery_head=head,
            lease=lease,
        ):
            raise ReleaseBlocked(
                "global Finance migration deploy lease allows only its exact "
                "owner-bound recovery PR"
            )
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
    lease_state = finance_deploy_lease_state(api, now=now)
    lease_recovery_pr = 0
    if lease_state.get("global_release_blocked") is True:
        if agent_gate is not None or ui_gate is not None:
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": (
                    "Finance deploy lease conflicts with an awaiting deploy/UI gate"
                ),
            }
        if lease_state.get("status") != "active":
            return {
                "status": "finance-deploy-lease-fail-closed",
                "found": False,
                "reason": ", ".join(
                    str(item)
                    for item in lease_state.get("ambiguous_reasons") or []
                )
                or str(lease_state.get("status") or "ambiguous"),
            }
        lease = lease_state.get("lease")
        if not isinstance(lease, Mapping):
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": "Finance deploy lease identity is unavailable",
            }
        authorized: list[int] = []
        for item in api.list_issues_by_label(
            FINANCE_DEPLOY_LEASE_RECOVERY_LABEL,
            state="open",
        ):
            if "pull_request" not in item:
                continue
            number = int(item.get("number") or 0)
            pull = api.get_pull(number)
            head = str((pull.get("head") or {}).get("sha") or "").lower()
            if _finance_recovery_authorization_matches(
                api,
                recovery_pr=number,
                recovery_head=head,
                lease=lease,
            ):
                authorized.append(number)
        if len(authorized) > 1:
            return {
                "status": "gate-conflict",
                "found": False,
                "reason": "multiple Finance owner-bound recovery PRs are authorized",
            }
        lease_recovery_pr = authorized[0] if authorized else 0
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
    if lease_state.get("global_release_blocked") is True:
        ready = [
            item
            for item in ready
            if int(item.get("number") or 0) == lease_recovery_pr
        ]
        if not ready:
            lease = lease_state.get("lease") or {}
            return {
                "status": "finance-deploy-lease",
                "found": False,
                "finance_lease_pr_number": int(
                    lease.get("anchor_pr") or 0
                ),
                "finance_lease_id": str(lease.get("lease_id") or ""),
                "finance_lease_revision": int(
                    lease.get("revision") or 0
                ),
            }
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


def _production_mutation_proof_for_command(
    api: ReleaseApi,
    command: ProductionMutationTerminalizationCommand,
    proof_values: Mapping[str, object],
) -> bool:
    expected = {key: str(value) for key, value in proof_values.items()}
    for fields in _repo_owned_marker_fields(
        api,
        command.pr,
        PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
    ):
        if all(fields.get(key) == value for key, value in expected.items()):
            deployment_evidence = str(fields.get("deployment_evidence") or "")
            try:
                _sha256_fingerprint(
                    deployment_evidence,
                    "deployment_evidence",
                )
            except ReleaseBlocked:
                continue
            return True
    return False


def production_mutation_terminalization_preflight(
    api: ReleaseApi,
    number: int,
    command: ProductionMutationTerminalizationCommand,
    *,
    actor: str,
    association: str,
) -> dict[str, Any]:
    """Validate immutable human/deploy/reconciliation identities before secrets are exposed."""

    normalized_association = association.upper()
    if normalized_association not in PRODUCTION_MUTATION_TERMINAL_ASSOCIATIONS:
        raise ReleaseBlocked(
            "production-mutation terminalization requires OWNER or MEMBER association"
        )
    if not actor or actor in {"github-actions", "github-actions[bot]"}:
        raise ReleaseBlocked(
            "production-mutation terminalization requires a non-bot command actor"
        )
    if command.pr != number:
        raise ReleaseBlocked(
            "production-mutation terminalization must target the current PR"
        )

    pull = api.get_pull(number)
    labels = label_names(pull)
    if task_class_from_labels(labels) != STANDARD_TASK_LABEL:
        raise ReleaseBlocked(
            "production-mutation terminalization requires task:standard"
        )
    if scope_from_labels(labels) != PRODUCTION_MUTATION_LABEL:
        raise ReleaseBlocked(
            "production-mutation terminalization requires scope:production-mutation"
        )
    state = release_state_from_labels(labels)
    if state not in {BLOCKED_LABEL, HALTED_LABEL, PRODUCTION_LABEL}:
        raise ReleaseBlocked(
            "production-mutation terminalization requires the fail-closed blocked/halted "
            "state or its already-proven terminal result"
        )
    actual_head = str((pull.get("head") or {}).get("sha") or "").lower()
    actual_merge = str(pull.get("merge_commit_sha") or "").lower()
    if actual_head != command.head_sha:
        raise ReleaseBlocked(
            "production-mutation terminalization head SHA is stale"
        )
    if not bool(pull.get("merged")) or actual_merge != command.merge_sha:
        raise ReleaseBlocked(
            "production-mutation terminalization merge SHA does not match the merged PR"
        )
    if not _has_successful_check(api, command.head_sha, "baseline"):
        raise ReleaseBlocked(
            "production-mutation terminalization requires successful baseline on exact head"
        )
    merged_at = _github_timestamp(pull.get("merged_at"))
    if merged_at is None:
        raise ReleaseBlocked(
            "production-mutation terminalization requires the exact GitHub merge timestamp"
        )

    gate_comment = _comment_by_id(
        api,
        number,
        command.gate_comment_id,
        "human gate",
    )
    gate_actor, gate_association = _comment_identity(
        gate_comment,
        field="human gate",
    )
    if _comment_body_digest(gate_comment) != command.gate_digest:
        raise ReleaseBlocked("human gate comment digest is stale")
    gate_time = _github_timestamp(gate_comment.get("created_at"))
    gate_body = str(gate_comment.get("body") or "")
    gate_folded = gate_body.casefold()
    if (
        gate_time is None
        or gate_time > merged_at
        or command.head_sha not in gate_body.lower()
        or not any(
            phrase in gate_folded
            for phrase in ("human gate", "human authorization", "user authorizes")
        )
    ):
        raise ReleaseBlocked(
            "human gate must be a pre-merge OWNER/MEMBER authorization bound to exact head"
        )

    reconciliation_comment = _comment_by_id(
        api,
        number,
        command.reconciliation_comment_id,
        "reconciliation",
    )
    reconciliation_actor, reconciliation_association = _comment_identity(
        reconciliation_comment,
        field="reconciliation",
    )
    if _comment_body_digest(reconciliation_comment) != command.reconciliation_digest:
        raise ReleaseBlocked("reconciliation comment digest is stale")
    reconciliation_time = _github_timestamp(reconciliation_comment.get("created_at"))
    reconciliation_body = str(reconciliation_comment.get("body") or "")
    reconciliation_folded = reconciliation_body.casefold()
    if (
        reconciliation_time is None
        or reconciliation_time < merged_at
        or command.deployed_sha not in reconciliation_body.lower()
        or command.evidence_fingerprint[7:] not in reconciliation_body.lower()
        or "reconciliation" not in reconciliation_folded
        or "complete" not in reconciliation_folded
    ):
        raise ReleaseBlocked(
            "reconciliation must be post-merge, bind deployed SHA and contain the exact "
            "evidence fingerprint"
        )

    comparison = api.compare(command.merge_sha, command.deployed_sha)
    if (
        str(comparison.get("status") or "") not in {"ahead", "identical"}
        or int(comparison.get("behind_by") or 0) != 0
    ):
        raise ReleaseBlocked(
            "deployed SHA must be the exact PR merge or a verified descendant"
        )

    proof_values: dict[str, object] = {
        "actor": actor,
        "association": normalized_association,
        "deployed": command.deployed_sha,
        "evidence": command.evidence_fingerprint,
        "gate_actor": gate_actor,
        "gate_association": gate_association,
        "gate_digest": command.gate_digest,
        "gate_id": command.gate_comment_id,
        "head": command.head_sha,
        "merge": command.merge_sha,
        "pr": number,
        "reconciliation_actor": reconciliation_actor,
        "reconciliation_association": reconciliation_association,
        "reconciliation_digest": command.reconciliation_digest,
        "reconciliation_id": command.reconciliation_comment_id,
    }
    already_completed = (
        PRODUCTION_LABEL in labels
        and _production_mutation_proof_for_command(api, command, proof_values)
    )
    return {
        "status": "already-completed" if already_completed else "ready",
        "pr": number,
        "head": command.head_sha,
        "merge": command.merge_sha,
        "deployed": command.deployed_sha,
        "already_completed": already_completed,
        "proof_values": proof_values,
    }


def complete_production_mutation_release(
    api: ReleaseApi,
    number: int,
    command: ProductionMutationTerminalizationCommand,
    deploy_evidence: Mapping[str, Any] | None,
    *,
    actor: str,
    association: str,
    actions_owned: bool,
) -> str:
    """Terminalize only after trusted-main Actions validates every exact evidence edge."""

    if not actions_owned:
        raise ReleaseBlocked(
            "production-mutation terminalization is restricted to trusted-main GitHub Actions"
        )
    plan = production_mutation_terminalization_preflight(
        api,
        number,
        command,
        actor=actor,
        association=association,
    )
    if plan["already_completed"]:
        api.dispatch_workflow("release-train.yml", "main")
        return "already-completed"
    if not isinstance(deploy_evidence, Mapping):
        raise ReleaseBlocked(
            "production-mutation terminalization requires repo-owned deployment evidence"
        )
    required_deploy = {
        "healthy": True,
        "status": "reconciled",
        "pr": number,
        "head": command.head_sha,
        "merge": command.deployed_sha,
        "expected_sha": command.deployed_sha,
        "target_id": CANONICAL_PRODUCTION_TARGET_ID,
        "read_only": True,
        "repairs_applied": False,
    }
    mismatches = [
        key for key, value in required_deploy.items() if deploy_evidence.get(key) != value
    ]
    if mismatches:
        raise ReleaseBlocked(
            "production deployment evidence does not match exact PR/head/deployed/target: "
            + ", ".join(mismatches)
        )
    deployment_evidence_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            deploy_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof_values = {
        **dict(plan["proof_values"]),
        "deployment_evidence": deployment_evidence_digest,
    }
    proof = _proof_marker(
        PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
        **proof_values,
    )
    if not _has_comment_proof(
        api,
        number,
        PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
        **proof_values,
    ):
        api.add_comment(
            number,
            "Release Train verified human-gated production-mutation completion for "
            f"head `{command.head_sha}`, merge `{command.merge_sha}` and deployed SHA "
            f"`{command.deployed_sha}` with reconciliation evidence "
            f"`{command.evidence_fingerprint}`.\n\n{proof}",
        )
    labels = label_names(api.get_pull(number))
    set_release_state(api, number, PRODUCTION_LABEL, current_labels=labels)
    api.dispatch_workflow("release-train.yml", "main")
    return PRODUCTION_LABEL


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


def production_mutation_terminal_state_proven(
    api: ReleaseApi,
    pull: Mapping[str, Any],
) -> bool:
    """Verify the Actions-owned terminal marker and its still-exact source comments."""

    number = int(pull.get("number") or 0)
    labels = label_names(pull)
    if number <= 0 or PRODUCTION_LABEL not in labels:
        return False
    for fields in _repo_owned_marker_fields(
        api,
        number,
        PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER,
    ):
        try:
            command = ProductionMutationTerminalizationCommand(
                pr=int(fields.get("pr") or 0),
                head_sha=_exact_sha(str(fields.get("head") or ""), "head"),
                merge_sha=_exact_sha(str(fields.get("merge") or ""), "merge"),
                deployed_sha=_exact_sha(
                    str(fields.get("deployed") or ""),
                    "deployed",
                ),
                gate_comment_id=int(fields.get("gate_id") or 0),
                gate_digest=_sha256_fingerprint(
                    str(fields.get("gate_digest") or ""),
                    "gate_digest",
                ),
                reconciliation_comment_id=int(
                    fields.get("reconciliation_id") or 0
                ),
                reconciliation_digest=_sha256_fingerprint(
                    str(fields.get("reconciliation_digest") or ""),
                    "reconciliation_digest",
                ),
                evidence_fingerprint=_sha256_fingerprint(
                    str(fields.get("evidence") or ""),
                    "evidence",
                ),
            )
            _sha256_fingerprint(
                str(fields.get("deployment_evidence") or ""),
                "deployment_evidence",
            )
            plan = production_mutation_terminalization_preflight(
                api,
                number,
                command,
                actor=str(fields.get("actor") or ""),
                association=str(fields.get("association") or ""),
            )
            expected = {
                key: str(value)
                for key, value in dict(plan["proof_values"]).items()
            }
            if not all(fields.get(key) == value for key, value in expected.items()):
                continue
            return True
        except (ReleaseBlocked, TypeError, ValueError):
            continue
    return False


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
        if scope == PRODUCTION_MUTATION_LABEL:
            return production_mutation_terminal_state_proven(api, pull)
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
        if status == "superseded-iteration":
            # Exact-SHA reconciliation proved this merged recovery healthy, but a
            # previous iteration still owns the active UI gate.  Keeping the
            # recovered iteration halted would globally deadlock the next
            # same-root recovery even though the durable gate remains intact.
            api.remove_label(number, HALTED_LABEL)
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
    if len(parts) == 6 and parts[:3] == ["/wb-core", "loop", "retry-blocked"]:
        try:
            command_number = int(parts[3])
        except ValueError as exc:
            raise ReleaseBlocked("invalid LOOP retry PR number") from exc
        if command_number != number or parts[4] != "head":
            raise ReleaseBlocked("LOOP retry must bind the current PR and exact head")
        _require_loop_operator(association)
        if task_class_from_labels(label_names(api.get_pull(number))) != LOOP_TASK_LABEL:
            raise ReleaseBlocked("LOOP retry command requires task:loop")
        return retry_blocked_release(
            api,
            number,
            expected_head_sha=parts[5],
            check_name="baseline",
        )
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


def handle_production_mutation_comment(
    api: ReleaseApi,
    number: int,
    command_text: str,
    *,
    actor: str,
    association: str,
    deploy_evidence: Mapping[str, Any] | None,
    actions_owned: bool,
) -> str:
    command = parse_production_mutation_terminalization_command(command_text)
    return complete_production_mutation_release(
        api,
        number,
        command,
        deploy_evidence,
        actor=actor,
        association=association,
        actions_owned=actions_owned,
    )


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
            "finance_lease_pr_number": result.get(
                "finance_lease_pr_number",
                "",
            ),
            "finance_lease_id": result.get("finance_lease_id", ""),
            "finance_lease_revision": result.get(
                "finance_lease_revision",
                "",
            ),
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


def command_preflight_production_mutation(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        command = parse_production_mutation_terminalization_command(args.command)
        plan = production_mutation_terminalization_preflight(
            api,
            args.pr,
            command,
            actor=args.actor,
            association=args.association,
        )
    except ReleaseBlocked as exc:
        _json_print({"status": "rejected", "pr_number": args.pr, "reason": str(exc)})
        return 2
    write_github_output(
        args.output_path,
        {
            "head_sha": command.head_sha,
            "merge_sha": command.merge_sha,
            "deployed_sha": command.deployed_sha,
            "already_completed": plan["already_completed"],
        },
    )
    _json_print(
        {
            "status": plan["status"],
            "pr_number": args.pr,
            "head_sha": command.head_sha,
            "merge_sha": command.merge_sha,
            "deployed_sha": command.deployed_sha,
        }
    )
    return 0


def command_preflight_finance_lease(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        command = parse_finance_deploy_lease_command(args.command)
        if command.anchor_pr != args.pr:
            raise ReleaseBlocked(
                "Finance deploy-lease command must target its anchor PR"
            )
        _validate_finance_deploy_lease_actor(
            actor=args.actor,
            association=args.association,
        )
        needs_deploy_readback = command.operation != "authorize-recovery"
        head_sha = command.head_sha
        if command.operation == "acquire":
            _finance_deploy_lease_anchor_preflight(api, command)
        else:
            state = finance_deploy_lease_state(api)
            if (
                state.get("status") not in {"active", "stale"}
                and command.operation not in {"release", "abort"}
            ):
                raise ReleaseBlocked(
                    "Finance deploy-lease command requires one unambiguous active/stale lease"
                )
            if state.get("status") in {"active", "stale"}:
                lease = _finance_deploy_lease_matches_command(
                    state,
                    command,
                    ignore_revision=command.operation in {"rebind", "resume"},
                )
                current_revision = int(lease.get("revision") or 0)
                if (
                    command.operation in {"rebind", "resume"}
                    and current_revision
                    not in {command.revision, command.revision + 1}
                ):
                    raise ReleaseBlocked(
                        "Finance deploy-lease rebind/resume revision is stale"
                    )
            else:
                pull = api.get_pull(command.anchor_pr)
                head_sha = str((pull.get("head") or {}).get("sha") or "")
                lease = {"head_sha": head_sha}
            head_sha = str(lease.get("head_sha") or "")
    except ReleaseBlocked as exc:
        _json_print({"status": "rejected", "pr_number": args.pr, "reason": str(exc)})
        return 2
    write_github_output(
        args.output_path,
        {
            "operation": command.operation,
            "head_sha": head_sha,
            "deployed_sha": command.deployed_sha,
            "needs_deploy_readback": needs_deploy_readback,
        },
    )
    _json_print(
        {
            "status": "ready",
            "pr_number": args.pr,
            "operation": command.operation,
            "head_sha": head_sha,
            "deployed_sha": command.deployed_sha,
            "needs_deploy_readback": needs_deploy_readback,
        }
    )
    return 0


def _read_json_object(path: Path | None, *, field: str) -> Mapping[str, Any] | None:
    if path is None:
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ReleaseBlocked(f"{field} must contain a JSON object")
    return loaded


def command_handle_finance_lease(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        command = parse_finance_deploy_lease_command(args.command)
        if command.anchor_pr != args.pr:
            raise ReleaseBlocked(
                "Finance deploy-lease command must target its anchor PR"
            )
        deploy_evidence = _read_json_object(
            args.deploy_evidence_file,
            field="Finance deploy evidence",
        )
        actions_owned = (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") == "issue_comment"
        )
        if command.operation == "acquire":
            status = acquire_finance_deploy_lease(
                api,
                command,
                deploy_evidence,
                actor=args.actor,
                association=args.association,
                actions_owned=actions_owned,
            )
        elif command.operation == "authorize-recovery":
            status = authorize_finance_deploy_lease_recovery(
                api,
                command,
                actor=args.actor,
                association=args.association,
                actions_owned=actions_owned,
            )
        elif command.operation in {"rebind", "resume"}:
            status = rebind_finance_deploy_lease(
                api,
                command,
                deploy_evidence,
                actor=args.actor,
                association=args.association,
                actions_owned=actions_owned,
            )
        else:
            status = terminalize_finance_deploy_lease(
                api,
                command,
                deploy_evidence,
                actor=args.actor,
                association=args.association,
                actions_owned=actions_owned,
            )
    except ReleaseBlocked as exc:
        _json_print({"status": "rejected", "pr_number": args.pr, "reason": str(exc)})
        return 2
    _json_print({"status": status, "pr_number": args.pr})
    return 0


def _write_private_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    if target == ROOT or ROOT in target.parents:
        raise ReleaseBlocked(
            "Finance deploy-lease readback must stay outside the Git checkout"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def command_finance_lease_status(args: argparse.Namespace) -> int:
    api = _api_from_env()
    state = finance_deploy_lease_state(api)
    if args.output is not None:
        _write_private_json_file(args.output, state)
    _json_print(state)
    if args.require_active and (
        state.get("status") != "active"
        or state.get("allows_finance_migration") is not True
        or state.get("ambiguous_reasons") not in ([], ())
    ):
        return 2
    return 0


def command_handle_comment(args: argparse.Namespace) -> int:
    api = _api_from_env()
    try:
        if args.command.strip().startswith("/wb-core production-mutation "):
            deploy_evidence: Mapping[str, Any] | None = None
            if args.deploy_evidence_file is not None:
                loaded = json.loads(
                    args.deploy_evidence_file.read_text(encoding="utf-8")
                )
                if not isinstance(loaded, dict):
                    raise ReleaseBlocked(
                        "deployment evidence must be a JSON object"
                    )
                deploy_evidence = loaded
            status = handle_production_mutation_comment(
                api,
                args.pr,
                args.command,
                actor=args.actor,
                association=args.association,
                deploy_evidence=deploy_evidence,
                actions_owned=(
                    os.environ.get("GITHUB_ACTIONS") == "true"
                    and os.environ.get("GITHUB_EVENT_NAME") == "issue_comment"
                ),
            )
        else:
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

    preflight_production_mutation = subparsers.add_parser(
        "preflight-production-mutation"
    )
    preflight_production_mutation.add_argument("--pr", type=int, required=True)
    preflight_production_mutation.add_argument("--command", required=True)
    preflight_production_mutation.add_argument("--actor", required=True)
    preflight_production_mutation.add_argument("--association", required=True)
    preflight_production_mutation.add_argument(
        "--output-path",
        default=os.environ.get("GITHUB_OUTPUT"),
    )
    preflight_production_mutation.set_defaults(
        handler=command_preflight_production_mutation
    )

    preflight_finance_lease = subparsers.add_parser(
        "preflight-finance-lease"
    )
    preflight_finance_lease.add_argument("--pr", type=int, required=True)
    preflight_finance_lease.add_argument("--command", required=True)
    preflight_finance_lease.add_argument("--actor", required=True)
    preflight_finance_lease.add_argument("--association", required=True)
    preflight_finance_lease.add_argument(
        "--output-path",
        default=os.environ.get("GITHUB_OUTPUT"),
    )
    preflight_finance_lease.set_defaults(
        handler=command_preflight_finance_lease
    )

    handle_finance_lease = subparsers.add_parser("handle-finance-lease")
    handle_finance_lease.add_argument("--pr", type=int, required=True)
    handle_finance_lease.add_argument("--command", required=True)
    handle_finance_lease.add_argument("--actor", required=True)
    handle_finance_lease.add_argument("--association", required=True)
    handle_finance_lease.add_argument("--deploy-evidence-file", type=Path)
    handle_finance_lease.set_defaults(handler=command_handle_finance_lease)

    finance_lease_status = subparsers.add_parser("finance-lease-status")
    finance_lease_status.add_argument("--output", type=Path)
    finance_lease_status.add_argument("--require-active", action="store_true")
    finance_lease_status.set_defaults(handler=command_finance_lease_status)

    handle_comment = subparsers.add_parser("handle-comment")
    handle_comment.add_argument("--pr", type=int, required=True)
    handle_comment.add_argument("--command", required=True)
    handle_comment.add_argument("--actor", required=True)
    handle_comment.add_argument("--association", required=True)
    handle_comment.add_argument("--deploy-evidence-file", type=Path)
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
