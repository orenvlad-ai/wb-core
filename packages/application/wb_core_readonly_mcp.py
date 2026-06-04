"""Read-only MCP boundary for the local wb-core checkout.

This module intentionally uses only the Python standard library.  It exposes a
small MCP-compatible JSON-RPC stdio server and keeps all repository access
behind one read policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, BinaryIO, Callable, Iterable, TextIO
from urllib.parse import urlparse


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "wb-core-readonly-mcp"
SERVER_VERSION = "0.1.0"

ALLOWED_TOP_LEVEL_DIRS = {
    "apps",
    "docs",
    "migration",
    "packages",
    "registry",
    "static",
    "templates",
    "tests",
}
ALLOWED_TOP_LEVEL_FILES = {"README.md", "AGENTS.md"}
DENIED_TOP_LEVEL_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "logs",
    "node_modules",
    "venv",
    "wb_core_docs_master",
}
DENIED_DIR_PARTS = {
    ".secrets",
    "__pycache__",
    "cookies",
    "secrets",
    "sessions",
    "tokens",
}
DENIED_FILE_SUFFIXES = {
    ".bak",
    ".cer",
    ".crt",
    ".db",
    ".dump",
    ".key",
    ".p12",
    ".pfx",
    ".pem",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
ARTIFACT_TEXT_SUFFIXES = {
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".timer",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
ARTIFACT_ALLOWED_PARTS = {"config", "configs", "fixture", "fixtures", "input", "inputs"}


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@")
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|cookie|session|authorization)"
    r"(\s*[:=]\s*)([\"']?)([^\"'\s,;}]+)([\"']?)"
)


@dataclass(frozen=True)
class ReadonlyMcpConfig:
    repo_root: Path
    source_mode: str = "local_checkout"
    repo_url: str | None = None
    branch: str | None = None
    refresh_policy: str = "none"
    remote_auth_token_env: str | None = None
    max_file_bytes: int = 1_048_576
    max_response_chars: int = 262_144
    max_range_lines: int = 400
    max_search_matches: int = 50
    max_find_results: int = 200
    max_tree_items: int = 500
    max_tree_depth: int = 3
    max_snippet_chars: int = 240

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_repo_root: Path | None = None) -> "ReadonlyMcpConfig":
        repo_root_value = data.get("repo_root")
        repo_root = Path(repo_root_value).expanduser() if repo_root_value else fallback_repo_root
        if repo_root is None:
            repo_root = find_repo_root(Path.cwd())
        return cls(
            repo_root=repo_root.resolve(),
            source_mode=str(data.get("source_mode") or "local_checkout"),
            repo_url=_optional_str(data.get("repo_url")),
            branch=_optional_str(data.get("branch")),
            refresh_policy=str(data.get("refresh_policy") or "none"),
            remote_auth_token_env=_optional_str(data.get("remote_auth_token_env")),
            max_file_bytes=_positive_int(data.get("max_file_bytes"), 1_048_576),
            max_response_chars=_positive_int(
                data.get("max_response_chars", data.get("max_response_bytes")),
                262_144,
            ),
            max_range_lines=_positive_int(data.get("max_range_lines"), 400),
            max_search_matches=_positive_int(data.get("max_search_matches"), 50),
            max_find_results=_positive_int(data.get("max_find_results"), 200),
            max_tree_items=_positive_int(data.get("max_tree_items"), 500),
            max_tree_depth=_positive_int(data.get("max_tree_depth"), 3),
            max_snippet_chars=_positive_int(data.get("max_snippet_chars"), 240),
        )


class PolicyError(Exception):
    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class ResolvedRepoPath:
    requested_path: str
    abs_path: Path
    rel_path: Path

    @property
    def rel_posix(self) -> str:
        return self.rel_path.as_posix()


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() and (candidate / "README.md").exists():
            return candidate
    raise PolicyError("repo_root_not_found", "Could not find wb-core repo root from current directory")


def load_config(config_path: Path | None, *, repo_root_override: Path | None = None) -> ReadonlyMcpConfig:
    data: dict[str, Any] = {}
    if config_path is not None:
        with config_path.expanduser().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    if repo_root_override is not None:
        data["repo_root"] = str(repo_root_override)
    return ReadonlyMcpConfig.from_dict(data)


def validate_managed_clone_config(
    config: ReadonlyMcpConfig,
    *,
    allow_local_checkout_for_smoke: bool = False,
) -> None:
    if allow_local_checkout_for_smoke and config.source_mode == "local_checkout":
        return
    if config.source_mode != "managed_clone":
        raise PolicyError(
            "invalid_remote_source_mode",
            "Remote HTTP MCP mode requires source_mode=managed_clone",
        )
    if not config.repo_url:
        raise PolicyError("invalid_remote_config", "Remote managed-clone config requires repo_url")
    if not config.branch:
        raise PolicyError("invalid_remote_config", "Remote managed-clone config requires branch")
    if config.refresh_policy not in {"none", "external_manual", "external_managed"}:
        raise PolicyError(
            "invalid_remote_config",
            "refresh_policy must be one of: none, external_manual, external_managed",
        )
    forbidden_roots = (Path("/opt/wb-core-runtime"), Path("/opt/wb-ai"))
    for forbidden in forbidden_roots:
        try:
            common = os.path.commonpath([str(forbidden), str(config.repo_root)])
        except ValueError:
            continue
        if common == str(forbidden):
            raise PolicyError(
                "denied_runtime_repo_root",
                "Remote MCP repo_root must be a separate managed clone, not production runtime/app state",
            )


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError("invalid_config", f"Expected positive integer config value, got {value!r}") from exc
    if parsed <= 0:
        raise PolicyError("invalid_config", f"Expected positive integer config value, got {value!r}")
    return parsed


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip()
    return parsed or None


def redact_text(text: str) -> str:
    redacted = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = URL_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}[REDACTED]@", redacted)
    redacted = ASSIGNMENT_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED]{match.group(5)}",
        redacted,
    )
    return redacted


def is_binary_bytes(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _safe_rel_string(path: str | None) -> str:
    if path is None or path == "":
        return "."
    return path


def _decode_text(data: bytes, *, path: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError("denied_binary", "Binary or non-UTF-8 file content is not returned", path=path) from exc


def _limit_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 32:
        return text[:max_chars], True
    return text[: max_chars - 32] + "\n[TRUNCATED_BY_RESPONSE_LIMIT]", True


class RepoReadService:
    def __init__(self, config: ReadonlyMcpConfig) -> None:
        self.config = config
        self.repo_root = config.repo_root.resolve()
        if not self.repo_root.exists() or not self.repo_root.is_dir():
            raise PolicyError("repo_root_not_found", f"Configured repo_root does not exist: {self.repo_root}")

    def repo_status(self) -> dict[str, Any]:
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"], allow_failure=True)
        commit = self._git(["rev-parse", "HEAD"], allow_failure=True)
        upstream = self._git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], allow_failure=True)
        origin_url = self._git(["config", "--get", "remote.origin.url"], allow_failure=True)
        status_output = self._git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            allow_failure=True,
            strip=False,
        )
        dirty: list[dict[str, str]] = []
        omitted_sensitive = 0
        omitted_not_allowed = 0

        for line in status_output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2]
            raw_path = line[3:]
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1]
            try:
                rel = Path(raw_path)
                self._check_rel_policy(rel)
            except PolicyError as exc:
                if exc.code.startswith("denied_"):
                    omitted_sensitive += 1
                else:
                    omitted_not_allowed += 1
                continue
            dirty.append({"status": status, "path": rel.as_posix()})

        return {
            "ok": True,
            "repo_root": str(self.repo_root),
            "source_mode": self.config.source_mode,
            "configured_repo_url": self.config.repo_url,
            "configured_branch": self.config.branch,
            "refresh_policy": self.config.refresh_policy,
            "branch": branch or None,
            "commit": commit or None,
            "upstream": upstream or None,
            "origin_url": redact_text(origin_url) or None,
            "auto_fetch": False,
            "last_fetch_head_mtime_epoch": self._fetch_head_mtime_epoch(),
            "dirty": dirty,
            "dirty_count": len(dirty),
            "omitted_sensitive_or_denied_count": omitted_sensitive,
            "omitted_not_allowed_count": omitted_not_allowed,
        }

    def list_tree(self, path: str = ".", max_depth: int | None = None, max_items: int | None = None) -> dict[str, Any]:
        max_depth = min(_positive_int(max_depth, self.config.max_tree_depth), self.config.max_tree_depth)
        max_items = min(_positive_int(max_items, self.config.max_tree_items), self.config.max_tree_items)
        base = self._resolve_existing(path, allow_root=True)
        if not base.abs_path.is_dir():
            return {
                "ok": False,
                "error": "not_a_directory",
                "message": "list_tree requires a directory path",
                "path": base.rel_posix,
            }

        entries: list[dict[str, Any]] = []
        omitted_denied = 0
        stack: list[tuple[Path, int]] = [(base.rel_path, 0)]
        while stack and len(entries) < max_items:
            rel_dir, depth = stack.pop()
            abs_dir = self.repo_root if rel_dir == Path(".") else self.repo_root / rel_dir
            try:
                children = sorted(abs_dir.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                omitted_denied += 1
                continue
            next_dirs: list[Path] = []
            for child in children:
                child_rel = child.relative_to(self.repo_root)
                try:
                    self._check_rel_policy(child_rel)
                    resolved_child = self._resolve_existing(child_rel.as_posix(), allow_root=False)
                except PolicyError:
                    omitted_denied += 1
                    continue
                entry = self._metadata_for_resolved(resolved_child, include_hash=False)
                entries.append(entry)
                if len(entries) >= max_items:
                    break
                if child.is_dir() and depth + 1 < max_depth:
                    next_dirs.append(child_rel)
            for rel_child_dir in reversed(next_dirs):
                stack.append((rel_child_dir, depth + 1))

        return {
            "ok": True,
            "path": "." if base.rel_path == Path(".") else base.rel_posix,
            "max_depth": max_depth,
            "entries": entries,
            "entry_count": len(entries),
            "omitted_denied_count": omitted_denied,
            "truncated": bool(stack or len(entries) >= max_items),
        }

    def find_files(self, pattern: str = "*", path: str = ".", max_results: int | None = None) -> dict[str, Any]:
        if not pattern or len(pattern) > 200:
            raise PolicyError("invalid_pattern", "find_files pattern must be 1..200 characters")
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PolicyError("invalid_pattern", "find_files pattern must be repo-relative and must not traverse")
        max_results = min(_positive_int(max_results, self.config.max_find_results), self.config.max_find_results)
        matches: list[dict[str, Any]] = []
        omitted_denied = 0
        for resolved in self._walk_files(path):
            rel_posix = resolved.rel_posix
            if "/" in pattern:
                matched = fnmatch(rel_posix, pattern)
            else:
                matched = fnmatch(resolved.rel_path.name, pattern)
            if not matched:
                continue
            try:
                matches.append(self._metadata_for_resolved(resolved, include_hash=False))
            except PolicyError:
                omitted_denied += 1
                continue
            if len(matches) >= max_results:
                break
        return {
            "ok": True,
            "pattern": pattern,
            "path": _safe_rel_string(path),
            "matches": matches,
            "match_count": len(matches),
            "omitted_denied_count": omitted_denied,
            "truncated": len(matches) >= max_results,
        }

    def search_text(
        self,
        query: str,
        path: str = ".",
        regex: bool = False,
        case_sensitive: bool = False,
        max_matches: int | None = None,
    ) -> dict[str, Any]:
        if not query or len(query) > 500:
            raise PolicyError("invalid_query", "search_text query must be 1..500 characters")
        if regex and len(query) > 200:
            raise PolicyError("invalid_query", "regex search query must be 1..200 characters")
        max_matches = min(_positive_int(max_matches, self.config.max_search_matches), self.config.max_search_matches)
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(query, flags) if regex else None
        literal = query if case_sensitive else query.lower()
        matches: list[dict[str, Any]] = []
        skipped_binary = 0
        skipped_large = 0
        skipped_denied = 0

        for resolved in self._walk_files(path):
            if len(matches) >= max_matches:
                break
            try:
                data = self._read_file_bytes(resolved)
                if is_binary_bytes(data[:4096]):
                    skipped_binary += 1
                    continue
                text = _decode_text(data, path=resolved.rel_posix)
            except PolicyError as exc:
                if exc.code == "denied_size_limit":
                    skipped_large += 1
                elif exc.code == "denied_binary":
                    skipped_binary += 1
                else:
                    skipped_denied += 1
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.lower()
                matched = compiled.search(line) is not None if compiled is not None else literal in haystack
                if not matched:
                    continue
                snippet = redact_text(line.strip())
                snippet, truncated = _limit_text(snippet, self.config.max_snippet_chars)
                matches.append(
                    {
                        "path": resolved.rel_posix,
                        "line": line_no,
                        "snippet": snippet,
                        "snippet_truncated": truncated,
                    }
                )
                if len(matches) >= max_matches:
                    break

        return {
            "ok": True,
            "query": query,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "matches": matches,
            "match_count": len(matches),
            "skipped_binary_count": skipped_binary,
            "skipped_large_count": skipped_large,
            "skipped_denied_count": skipped_denied,
            "truncated": len(matches) >= max_matches,
        }

    def read_file(self, path: str) -> dict[str, Any]:
        resolved = self._resolve_existing_file(path)
        data = self._read_file_bytes(resolved)
        if is_binary_bytes(data[:4096]):
            return {
                "ok": False,
                "error": "denied_binary",
                "message": "Binary file content is not returned",
                "path": resolved.rel_posix,
                "metadata": self._metadata_for_resolved(resolved, include_hash=False),
            }
        text = redact_text(_decode_text(data, path=resolved.rel_posix))
        text, truncated = _limit_text(text, self.config.max_response_chars)
        return {
            "ok": True,
            "path": resolved.rel_posix,
            "text": text,
            "truncated": truncated,
            "size_bytes": resolved.abs_path.stat().st_size,
        }

    def read_file_range(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        if start_line <= 0:
            raise PolicyError("invalid_range", "start_line must be positive", path=path)
        if end_line is None:
            end_line = start_line + self.config.max_range_lines - 1
        if end_line < start_line:
            raise PolicyError("invalid_range", "end_line must be >= start_line", path=path)
        if end_line - start_line + 1 > self.config.max_range_lines:
            raise PolicyError("invalid_range", f"line range exceeds max_range_lines={self.config.max_range_lines}", path=path)

        full = self.read_file(path)
        if not full.get("ok"):
            return full
        lines = str(full["text"]).splitlines()
        selected = lines[start_line - 1 : end_line]
        text = "\n".join(selected)
        text, truncated = _limit_text(text, self.config.max_response_chars)
        return {
            "ok": True,
            "path": full["path"],
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "text": text,
            "truncated": truncated or bool(full.get("truncated")),
            "total_lines_available": len(lines),
        }

    def get_file_metadata(self, path: str) -> dict[str, Any]:
        resolved = self._resolve_existing_file(path)
        metadata = self._metadata_for_resolved(resolved, include_hash=True)
        return {"ok": True, "metadata": metadata}

    def validate_managed_clone_alignment(self) -> None:
        if self.config.source_mode != "managed_clone":
            return
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"], allow_failure=True)
        if branch != self.config.branch:
            raise PolicyError(
                "managed_clone_branch_mismatch",
                f"Managed clone branch mismatch: expected {self.config.branch!r}, got {branch!r}",
            )
        origin_url = self._git(["config", "--get", "remote.origin.url"], allow_failure=True)
        if self.config.repo_url and origin_url != self.config.repo_url:
            raise PolicyError(
                "managed_clone_origin_mismatch",
                "Managed clone origin URL does not match configured repo_url",
            )
        dirty = self._git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            allow_failure=True,
            strip=False,
        )
        if dirty.strip():
            raise PolicyError(
                "managed_clone_dirty",
                "Remote MCP managed clone must be clean before serving repo files",
            )

    def _git(self, args: list[str], *, allow_failure: bool, strip: bool = True) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                check=not allow_failure,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            if allow_failure:
                return ""
            raise
        if completed.returncode != 0 and allow_failure:
            return ""
        return completed.stdout.strip() if strip else completed.stdout.rstrip("\n")

    def _fetch_head_mtime_epoch(self) -> int | None:
        fetch_head = self.repo_root / ".git" / "FETCH_HEAD"
        try:
            return int(fetch_head.stat().st_mtime)
        except OSError:
            return None

    def _resolve_existing_file(self, path: str) -> ResolvedRepoPath:
        resolved = self._resolve_existing(path, allow_root=False)
        if not resolved.abs_path.is_file():
            raise PolicyError("not_a_file", "Requested path is not a file", path=resolved.rel_posix)
        return resolved

    def _resolve_existing(self, path: str | Path, *, allow_root: bool) -> ResolvedRepoPath:
        rel = self._normalize_rel_path(path, allow_root=allow_root)
        if rel != Path("."):
            self._check_rel_policy(rel)
        candidate = self.repo_root if rel == Path(".") else self.repo_root / rel
        if not candidate.exists():
            raise PolicyError("not_found", "Requested path does not exist", path="." if rel == Path(".") else rel.as_posix())
        try:
            resolved_abs = candidate.resolve(strict=True)
        except OSError as exc:
            raise PolicyError("denied_symlink_escape", "Could not resolve path safely", path=rel.as_posix()) from exc
        if not self._is_within_repo(resolved_abs):
            raise PolicyError("denied_symlink_escape", "Symlink target escapes repo_root", path=rel.as_posix())
        resolved_rel = Path(os.path.relpath(resolved_abs, self.repo_root))
        if resolved_rel != Path("."):
            self._check_rel_policy(resolved_rel)
        return ResolvedRepoPath(
            requested_path=str(path),
            abs_path=resolved_abs,
            rel_path=resolved_rel,
        )

    def _normalize_rel_path(self, path: str | Path, *, allow_root: bool) -> Path:
        raw = str(path)
        if "\x00" in raw:
            raise PolicyError("invalid_path", "Path contains NUL byte")
        if raw in {"", "."}:
            if allow_root:
                return Path(".")
            raise PolicyError("invalid_path", "A file path is required")
        raw_path = Path(raw)
        if raw_path.is_absolute():
            raise PolicyError("denied_absolute_path", "Only repo-relative paths are allowed", path=raw)
        candidate = Path(os.path.normpath(str(self.repo_root / raw_path)))
        if not self._is_within_repo(candidate):
            raise PolicyError("denied_outside_repo", "Path escapes repo_root", path=raw)
        return Path(os.path.relpath(candidate, self.repo_root))

    def _is_within_repo(self, path: Path) -> bool:
        try:
            os.path.commonpath([str(self.repo_root), str(path)])
        except ValueError:
            return False
        return os.path.commonpath([str(self.repo_root), str(path)]) == str(self.repo_root)

    def _check_rel_policy(self, rel: Path) -> None:
        if rel == Path("."):
            return
        rel_posix = rel.as_posix()
        parts = rel.parts
        if not parts:
            return
        if any(part in {"", ".", ".."} for part in parts):
            raise PolicyError("invalid_path", "Path must be normalized", path=rel_posix)
        first = parts[0]
        name = rel.name
        lower_name = name.lower()
        lower_parts = [part.lower() for part in parts]

        if first in DENIED_TOP_LEVEL_DIRS:
            code = "denied_derived_pack" if first == "wb_core_docs_master" else "denied_generated_or_private_path"
            raise PolicyError(code, "Path is outside the read-only MCP scope", path=rel_posix)
        if any(part in DENIED_DIR_PARTS for part in lower_parts):
            raise PolicyError("denied_sensitive_path", "Sensitive directory path is denied", path=rel_posix)
        if lower_name == ".ds_store":
            raise PolicyError("denied_generated_or_private_path", "Generated local file is denied", path=rel_posix)
        if lower_name == ".env" or lower_name.startswith(".env.") or lower_name.endswith(".env"):
            raise PolicyError("denied_sensitive_path", "Env files are denied", path=rel_posix)
        if rel.suffix.lower() in DENIED_FILE_SUFFIXES:
            raise PolicyError("denied_sensitive_path", "Sensitive or generated file suffix is denied", path=rel_posix)
        if "storage-state" in lower_name or "storage_state" in lower_name:
            raise PolicyError("denied_sensitive_path", "Browser storage-state files are denied", path=rel_posix)
        if "credential" in lower_name or "service-account" in lower_name or "service_account" in lower_name:
            raise PolicyError("denied_sensitive_path", "Credential files are denied", path=rel_posix)
        if "user data" in {part.lower() for part in parts}:
            raise PolicyError("denied_browser_profile", "Browser profile paths are denied", path=rel_posix)
        if any(part.lower().startswith("profile ") for part in parts):
            raise PolicyError("denied_browser_profile", "Browser profile paths are denied", path=rel_posix)
        if "playwright" in lower_parts and ".auth" in lower_parts:
            raise PolicyError("denied_browser_profile", "Browser auth state paths are denied", path=rel_posix)

        if len(parts) == 1 and name in ALLOWED_TOP_LEVEL_FILES:
            return
        if first in ALLOWED_TOP_LEVEL_DIRS:
            return
        if first == "artifacts":
            if self._artifact_rel_allowed(rel):
                return
            raise PolicyError("denied_artifact_policy", "Only tracked fixture/config-like artifacts are in scope", path=rel_posix)
        raise PolicyError("denied_not_allowed", "Path is not in the allowed repository read scope", path=rel_posix)

    def _artifact_rel_allowed(self, rel: Path) -> bool:
        lower_parts = {part.lower() for part in rel.parts}
        if lower_parts & ARTIFACT_ALLOWED_PARTS:
            return True
        if rel.suffix.lower() in ARTIFACT_TEXT_SUFFIXES:
            return True
        return False

    def _read_file_bytes(self, resolved: ResolvedRepoPath) -> bytes:
        size = resolved.abs_path.stat().st_size
        if size > self.config.max_file_bytes:
            raise PolicyError(
                "denied_size_limit",
                f"File exceeds max_file_bytes={self.config.max_file_bytes}",
                path=resolved.rel_posix,
            )
        return resolved.abs_path.read_bytes()

    def _walk_files(self, path: str = ".") -> Iterable[ResolvedRepoPath]:
        base = self._resolve_existing(path, allow_root=True)
        if base.abs_path.is_file():
            yield self._resolve_existing_file(base.rel_posix)
            return
        for root, dirs, files in os.walk(base.abs_path, followlinks=False):
            root_path = Path(root)
            kept_dirs: list[str] = []
            for dirname in sorted(dirs):
                rel_dir = (root_path / dirname).relative_to(self.repo_root)
                try:
                    self._check_rel_policy(rel_dir)
                    resolved_dir = self._resolve_existing(rel_dir.as_posix(), allow_root=False)
                except PolicyError:
                    continue
                if resolved_dir.abs_path.is_dir():
                    kept_dirs.append(dirname)
            dirs[:] = kept_dirs

            for filename in sorted(files):
                rel_file = (root_path / filename).relative_to(self.repo_root)
                try:
                    yield self._resolve_existing_file(rel_file.as_posix())
                except PolicyError:
                    continue

    def _metadata_for_resolved(self, resolved: ResolvedRepoPath, *, include_hash: bool) -> dict[str, Any]:
        stat = resolved.abs_path.stat()
        if resolved.abs_path.is_dir():
            kind = "directory"
        elif resolved.abs_path.is_file():
            sample = resolved.abs_path.read_bytes()[:4096] if stat.st_size <= self.config.max_file_bytes else b""
            kind = "binary_file" if sample and is_binary_bytes(sample) else "text_file"
            if stat.st_size > self.config.max_file_bytes:
                kind = "large_file"
        else:
            kind = "other"
        payload: dict[str, Any] = {
            "path": resolved.rel_posix,
            "type": kind,
            "size_bytes": stat.st_size,
            "mtime_epoch": int(stat.st_mtime),
        }
        if include_hash and resolved.abs_path.is_file() and stat.st_size <= self.config.max_file_bytes:
            payload["sha256"] = hashlib.sha256(resolved.abs_path.read_bytes()).hexdigest()
        return payload


def build_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "repo_status",
            "description": "Report branch, commit and bounded dirty state for the configured wb-core checkout.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_tree",
            "description": "List allowed repository files/directories under a repo-relative path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "max_depth": {"type": "integer", "minimum": 1},
                    "max_items": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "find_files",
            "description": "Find allowed files by glob/name without returning file contents.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "*"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "search_text",
            "description": "Search allowed UTF-8 text files and return capped redacted snippets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "regex": {"type": "boolean", "default": False},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "max_matches": {"type": "integer", "minimum": 1},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_file",
            "description": "Read one allowed UTF-8 text file with redaction and response caps.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_file_range",
            "description": "Read an allowed UTF-8 text file line range with redaction and caps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_file_metadata",
            "description": "Return metadata for one allowed file without contents.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    ]


class McpJsonRpcServer:
    def __init__(self, service: RepoReadService) -> None:
        self.service = service
        self.tools: dict[str, Callable[..., dict[str, Any]]] = {
            "repo_status": lambda **_: self.service.repo_status(),
            "list_tree": self.service.list_tree,
            "find_files": self.service.find_files,
            "search_text": self.service.search_text,
            "read_file": self.service.read_file,
            "read_file_range": self.service.read_file_range,
            "get_file_metadata": self.service.get_file_metadata,
        }

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if request_id is None and method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                requested_version = params.get("protocolVersion") if isinstance(params, dict) else None
                result = {
                    "protocolVersion": requested_version or MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
                return self._result(request_id, result)
            if method == "tools/list":
                return self._result(request_id, {"tools": build_tool_definitions()})
            if method == "tools/call":
                if not isinstance(params, dict):
                    raise PolicyError("invalid_params", "tools/call params must be an object")
                name = str(params.get("name", ""))
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise PolicyError("invalid_params", "tools/call arguments must be an object")
                payload = self.call_tool(name, arguments)
                return self._result(request_id, self._tool_content(payload))
            if request_id is None:
                return None
            return self._error(request_id, -32601, f"Unknown method: {method}")
        except PolicyError as exc:
            if method == "tools/call":
                return self._result(request_id, self._tool_content(exc.payload()))
            return self._error(request_id, -32602, exc.message, {"code": exc.code, "path": exc.path})
        except Exception as exc:  # pragma: no cover - defensive protocol boundary.
            return self._error(request_id, -32603, "Internal server error", {"detail": str(exc)})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "error": "unknown_tool", "message": f"Unknown tool: {name}"}
        try:
            return tool(**arguments)
        except PolicyError as exc:
            return exc.payload()

    def _tool_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        text, truncated = _limit_text(text, self.service.config.max_response_chars)
        if truncated:
            payload = {
                "ok": False,
                "error": "response_truncated",
                "message": "Tool response exceeded max_response_chars after formatting",
                "partial": text,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


class ReadonlyMcpHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        mcp_server: McpJsonRpcServer,
        *,
        auth_token: str | None = None,
        mcp_path: str = "/mcp",
        sse_path: str = "/sse",
        health_path: str = "/healthz",
        max_request_bytes: int = 1_048_576,
    ) -> None:
        super().__init__(server_address, ReadonlyMcpHttpHandler)
        self.mcp_server = mcp_server
        self.auth_token = auth_token
        self.mcp_path = mcp_path
        self.sse_path = sse_path
        self.health_path = health_path
        self.max_request_bytes = max_request_bytes


class ReadonlyMcpHttpHandler(BaseHTTPRequestHandler):
    server: ReadonlyMcpHttpServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
        parsed = urlparse(self.path)
        if parsed.path == self.server.health_path:
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "transport": "http-jsonrpc",
                    "mcp_path": self.server.mcp_path,
                    "sse_path": self.server.sse_path,
                    "auth_required": self.server.auth_token is not None,
                },
            )
            return
        if parsed.path == self.server.sse_path:
            if not self._authorized():
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            body = (
                "event: endpoint\n"
                f"data: {self.server.mcp_path}\n\n"
                "event: ready\n"
                "data: {\"transport\":\"http-jsonrpc\",\"session\":\"stateless\"}\n\n"
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        parsed = urlparse(self.path)
        if parsed.path != self.server.mcp_path:
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            self._write_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "expected_json"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_content_length"})
            return
        if content_length <= 0:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "empty_request"})
            return
        if content_length > self.server.max_request_bytes:
            self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "request_too_large"})
            return
        raw = self.rfile.read(content_length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(request, dict):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_jsonrpc_request"})
            return
        response = self.server.mcp_server.handle_request(request)
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._write_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
        return

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if token is None:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def resolve_remote_auth_token(config: ReadonlyMcpConfig) -> str | None:
    if not config.remote_auth_token_env:
        return None
    token = os.environ.get(config.remote_auth_token_env, "")
    if not token:
        raise PolicyError(
            "missing_remote_auth_token",
            f"remote_auth_token_env is configured but {config.remote_auth_token_env} is empty",
        )
    return token


def build_http_server(
    config: ReadonlyMcpConfig,
    *,
    host: str,
    port: int,
    allow_local_checkout_for_smoke: bool = False,
) -> ReadonlyMcpHttpServer:
    validate_managed_clone_config(config, allow_local_checkout_for_smoke=allow_local_checkout_for_smoke)
    mcp_server = build_server(config)
    mcp_server.service.validate_managed_clone_alignment()
    auth_token = resolve_remote_auth_token(config)
    return ReadonlyMcpHttpServer(
        (host, port),
        mcp_server,
        auth_token=auth_token,
        max_request_bytes=max(config.max_response_chars, 65_536),
    )


def serve_http(
    config: ReadonlyMcpConfig,
    *,
    host: str,
    port: int,
    allow_local_checkout_for_smoke: bool = False,
) -> None:
    http_server = build_http_server(
        config,
        host=host,
        port=port,
        allow_local_checkout_for_smoke=allow_local_checkout_for_smoke,
    )
    try:
        http_server.serve_forever(poll_interval=0.25)
    finally:
        http_server.server_close()


def read_json_rpc_message(stream: BinaryIO) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii", errors="replace").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
    length_header = headers.get("content-length")
    if length_header is None:
        raise PolicyError("invalid_message", "Missing Content-Length header")
    length = int(length_header)
    body = stream.read(length)
    if len(body) != length:
        raise PolicyError("invalid_message", "Unexpected EOF while reading JSON-RPC body")
    return json.loads(body.decode("utf-8"))


def write_json_rpc_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def serve_stdio(server: McpJsonRpcServer, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    while True:
        request = read_json_rpc_message(input_stream)
        if request is None:
            return
        response = server.handle_request(request)
        if response is not None:
            write_json_rpc_message(output_stream, response)


def build_server(config: ReadonlyMcpConfig) -> McpJsonRpcServer:
    return McpJsonRpcServer(RepoReadService(config))


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MCP server for the local wb-core checkout.")
    parser.add_argument("--config", type=Path, help="Path to JSON config file.")
    parser.add_argument("--repo-root", type=Path, help="Override repo_root from config.")
    parser.add_argument("--call-tool", help="Call one tool once and print JSON instead of starting stdio MCP.")
    parser.add_argument("--arguments-json", default="{}", help="JSON object for --call-tool arguments.")
    args = parser.parse_args(argv)

    output = stdout or sys.stdout
    config = load_config(args.config, repo_root_override=args.repo_root)
    server = build_server(config)

    if args.call_tool:
        try:
            arguments = json.loads(args.arguments_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--arguments-json must be a JSON object: {exc}") from exc
        if not isinstance(arguments, dict):
            raise SystemExit("--arguments-json must be a JSON object")
        payload = server.call_tool(args.call_tool, arguments)
        output.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        output.write("\n")
        return 0

    serve_stdio(server)
    return 0


def http_main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTTP JSON-RPC MCP server for a managed read-only wb-core clone.")
    parser.add_argument("--config", type=Path, required=True, help="Path to JSON config file.")
    parser.add_argument("--repo-root", type=Path, help="Override repo_root from config.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Use a reverse proxy/tunnel for remote exposure.")
    parser.add_argument("--port", type=int, default=8766, help="Bind port.")
    parser.add_argument(
        "--allow-local-checkout-for-smoke",
        action="store_true",
        help="Test-only override; remote mode normally requires source_mode=managed_clone.",
    )
    args = parser.parse_args(argv)

    output = stdout or sys.stdout
    try:
        config = load_config(args.config, repo_root_override=args.repo_root)
        http_server = build_http_server(
            config,
            host=args.host,
            port=args.port,
            allow_local_checkout_for_smoke=args.allow_local_checkout_for_smoke,
        )
    except PolicyError as exc:
        sys.stderr.write(json.dumps(exc.payload(), ensure_ascii=False, sort_keys=True))
        sys.stderr.write("\n")
        return 2
    actual_host, actual_port = http_server.server_address
    auth_enabled = bool(config.remote_auth_token_env)
    output.write(
        json.dumps(
            {
                "ok": True,
                "server": SERVER_NAME,
                "transport": "http-jsonrpc",
                "url": f"http://{actual_host}:{actual_port}/mcp",
                "sse_url": f"http://{actual_host}:{actual_port}/sse",
                "source_mode": config.source_mode,
                "repo_root": str(config.repo_root),
                "repo_url": config.repo_url,
                "branch": config.branch,
                "refresh_policy": config.refresh_policy,
                "auth_required": auth_enabled,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    output.write("\n")
    output.flush()
    try:
        http_server.serve_forever(poll_interval=0.25)
    finally:
        http_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
