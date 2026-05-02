"""Repo-only execution loop contracts for the curator cockpit MVP."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Literal, Mapping, Sequence
import uuid

from packages.application.curator_cockpit_mvp import (
    CuratorCockpitValidationError,
    SprintStep,
    TaskSpec,
    build_codex_prompt,
    sprint_step_to_dict,
    sprint_steps_from_task_spec_mapping,
    task_spec_from_mapping,
    validate_sprint_step,
    validate_task_spec,
)

ExecutorMode = Literal["fake", "command"]
RunStatus = Literal["prepared", "running", "verifier_passed", "failed", "blocked", "human_gate_required"]
CheckStatus = Literal["passed", "failed", "skipped"]
VerifierStatus = Literal["passed", "failed", "blocked"]

EXECUTOR_MODES = {"fake", "command"}
RUN_STATUSES = {"prepared", "running", "verifier_passed", "failed", "blocked", "human_gate_required"}
CHECK_STATUSES = {"passed", "failed", "skipped"}
VERIFIER_STATUSES = {"passed", "failed", "blocked"}
MANDATORY_HANDOFF_BLOCKS = ("=== ДЛЯ КУРАТОРА ===", "=== СЖАТАЯ ПРОВЕРКА ===")
COMMAND_FORBIDDEN_TOKENS = ("live_deploy", "deploy", "ssh", "sudo", "root_shell")
RUN_METADATA_FILE = "run.json"


@dataclass(frozen=True)
class RunRequest:
    id: str
    task_spec_id: str
    step_id: str
    executor_mode: ExecutorMode
    repo_root: str
    state_dir: str
    base_ref: str
    branch_name: str | None = None
    allow_real_executor: bool = False
    executor_command: str | None = None
    created_at: str = field(default_factory=lambda: _now_utc())


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    command: str | None = None
    output_path: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class VerifierResult:
    status: VerifierStatus
    check_results: Sequence[CheckResult]
    changed_files: Sequence[str]
    forbidden_path_hits: Sequence[str]
    mandatory_handoff_blocks_present: bool
    blocker_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_results", tuple(self.check_results))
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "forbidden_path_hits", tuple(self.forbidden_path_hits))


@dataclass(frozen=True)
class RunResult:
    id: str
    status: RunStatus
    task_spec_id: str
    step_id: str
    branch_name: str | None
    worktree_path: str | None
    run_dir: str
    prompt_path: str
    handoff_path: str | None
    log_path: str | None
    changed_files: Sequence[str]
    check_results: Sequence[CheckResult]
    blocker_reason: str | None = None
    next_manual_step: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "check_results", tuple(self.check_results))


def prepare_run(
    task_spec_payload: Mapping[str, Any],
    *,
    step_id: str,
    repo_root: Path,
    state_dir: Path,
    base_ref: str | None = None,
    branch_name: str | None = None,
    executor_mode: ExecutorMode = "fake",
    allow_real_executor: bool = False,
    executor_command: str | None = None,
) -> RunResult:
    task_spec, step = _validated_task_and_step(task_spec_payload, step_id)
    _validate_executor_policy(task_spec, executor_mode, allow_real_executor, executor_command)

    repo_root = _resolve_repo_root(repo_root)
    base_ref = base_ref or _git_output(repo_root, "rev-parse", "HEAD")
    run_id = _new_run_id(task_spec, step)
    run_dir = state_dir / "runs" / run_id
    prompt_path = run_dir / "prompt.txt"
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt_path.write_text(build_codex_prompt(task_spec, step), encoding="utf-8")

    request = RunRequest(
        id=run_id,
        task_spec_id=task_spec.id,
        step_id=step.id,
        executor_mode=executor_mode,
        repo_root=str(repo_root),
        state_dir=str(state_dir),
        base_ref=base_ref,
        branch_name=branch_name,
        allow_real_executor=allow_real_executor,
        executor_command=_metadata_command(executor_mode, executor_command),
    )
    result = RunResult(
        id=run_id,
        status="prepared",
        task_spec_id=task_spec.id,
        step_id=step.id,
        branch_name=branch_name,
        worktree_path=None,
        run_dir=str(run_dir),
        prompt_path=str(prompt_path),
        handoff_path=None,
        log_path=None,
        changed_files=(),
        check_results=(),
    )
    _write_run_metadata(run_dir, request, task_spec_payload, step, result)
    return result


def run_step(
    task_spec_payload: Mapping[str, Any],
    *,
    step_id: str,
    repo_root: Path,
    state_dir: Path,
    base_ref: str | None = None,
    branch_name: str | None = None,
    executor_mode: ExecutorMode = "fake",
    allow_real_executor: bool = False,
    executor_command: str | None = None,
) -> RunResult:
    task_spec, step = _validated_task_and_step(task_spec_payload, step_id)
    _validate_executor_policy(task_spec, executor_mode, allow_real_executor, executor_command)

    repo_root = _resolve_repo_root(repo_root)
    base_ref = base_ref or _git_output(repo_root, "rev-parse", "HEAD")
    run_id = _new_run_id(task_spec, step)
    run_dir = state_dir / "runs" / run_id
    prompt_path = run_dir / "prompt.txt"
    handoff_path = run_dir / "handoff.txt"
    log_path = run_dir / "executor.log"
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt_path.write_text(build_codex_prompt(task_spec, step), encoding="utf-8")

    branch_name = branch_name or f"curator/run/{run_id}"
    request = RunRequest(
        id=run_id,
        task_spec_id=task_spec.id,
        step_id=step.id,
        executor_mode=executor_mode,
        repo_root=str(repo_root),
        state_dir=str(state_dir),
        base_ref=base_ref,
        branch_name=branch_name,
        allow_real_executor=allow_real_executor,
        executor_command=_metadata_command(executor_mode, executor_command),
    )
    result = RunResult(
        id=run_id,
        status="running",
        task_spec_id=task_spec.id,
        step_id=step.id,
        branch_name=branch_name,
        worktree_path=None,
        run_dir=str(run_dir),
        prompt_path=str(prompt_path),
        handoff_path=str(handoff_path),
        log_path=str(log_path),
        changed_files=(),
        check_results=(),
    )
    _write_run_metadata(run_dir, request, task_spec_payload, step, result)

    worktree_path = state_dir / "worktrees" / run_id
    try:
        _create_worktree(repo_root, worktree_path, branch_name, base_ref)
    except CuratorCockpitExecutionError as exc:
        blocked = replace(
            result,
            status="blocked",
            blocker_reason=str(exc),
            next_manual_step="Inspect git worktree state, branch refs and base_ref before retrying.",
        )
        _write_run_metadata(run_dir, request, task_spec_payload, step, blocked)
        return blocked

    result = replace(result, worktree_path=str(worktree_path))
    _write_run_metadata(run_dir, request, task_spec_payload, step, result)

    if executor_mode == "fake":
        _run_fake_executor(task_spec, step, result)
    else:
        _run_command_executor(worktree_path, handoff_path, log_path, executor_command or "")

    changed_files = _collect_changed_files(worktree_path)
    result = replace(result, changed_files=changed_files)
    _write_run_metadata(run_dir, request, task_spec_payload, step, result)

    verifier = verify_run(run_dir)
    status = _run_status_from_verifier(verifier)
    blocker_reason = verifier.blocker_reason
    if executor_mode == "command" and _command_failed(log_path):
        status = "failed"
        blocker_reason = "executor command exited non-zero"
    final = replace(
        result,
        status=status,
        changed_files=verifier.changed_files,
        check_results=verifier.check_results,
        blocker_reason=blocker_reason,
        next_manual_step=_next_manual_step(status, blocker_reason),
    )
    _write_run_metadata(run_dir, request, task_spec_payload, step, final)
    return final


def verify_run(run_dir: Path) -> VerifierResult:
    metadata = _read_run_metadata(run_dir)
    request = metadata["request"]
    result = metadata["result"]
    task_spec = task_spec_from_mapping(metadata["task_spec"])
    validate_task_spec(task_spec, require_frozen=True)

    run_dir = Path(str(result["run_dir"]))
    prompt_path = Path(str(result["prompt_path"]))
    handoff_raw = result.get("handoff_path")
    handoff_path = Path(str(handoff_raw)) if handoff_raw else None
    worktree_raw = result.get("worktree_path")
    worktree_path = Path(str(worktree_raw)) if worktree_raw else None
    checks_dir = run_dir / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)

    check_results: list[CheckResult] = []
    if prompt_path.exists():
        check_results.append(CheckResult(name="prompt_exists", status="passed", output_path=str(prompt_path)))
    else:
        check_results.append(CheckResult(name="prompt_exists", status="failed", reason="prompt file is missing"))

    handoff_text = ""
    if handoff_path and handoff_path.exists():
        handoff_text = handoff_path.read_text(encoding="utf-8")
        check_results.append(CheckResult(name="handoff_exists", status="passed", output_path=str(handoff_path)))
    else:
        check_results.append(CheckResult(name="handoff_exists", status="failed", reason="handoff file is missing"))

    mandatory_blocks_present = all(block in handoff_text for block in MANDATORY_HANDOFF_BLOCKS)
    check_results.append(
        CheckResult(
            name="handoff_mandatory_blocks",
            status="passed" if mandatory_blocks_present else "failed",
            output_path=str(handoff_path) if handoff_path else None,
            reason=None if mandatory_blocks_present else "mandatory handoff blocks are missing",
        )
    )

    changed_files = _merged_changed_files(result, worktree_path)
    forbidden_hits = _forbidden_path_hits(changed_files, task_spec.forbidden_paths)
    allowed_violations = _allowed_path_violations(changed_files, task_spec.allowed_paths)
    check_results.append(
        CheckResult(
            name="forbidden_paths",
            status="passed" if not forbidden_hits else "failed",
            reason=None if not forbidden_hits else f"forbidden path changes detected: {', '.join(forbidden_hits)}",
        )
    )
    check_results.append(
        CheckResult(
            name="allowed_paths",
            status="passed" if not allowed_violations else "failed",
            reason=None if not allowed_violations else f"changes outside allowed paths: {', '.join(allowed_violations)}",
        )
    )

    check_results.append(_fake_executor_policy_check(request))
    check_results.append(_git_diff_check(worktree_path, checks_dir))

    failed_checks = [check for check in check_results if check.status == "failed"]
    if forbidden_hits:
        status: VerifierStatus = "blocked"
        blocker_reason = f"forbidden path changes detected: {', '.join(forbidden_hits)}"
    elif failed_checks:
        status = "failed"
        blocker_reason = "; ".join(check.reason or check.name for check in failed_checks)
    else:
        status = "passed"
        blocker_reason = None

    verifier = VerifierResult(
        status=status,
        check_results=tuple(check_results),
        changed_files=tuple(changed_files),
        forbidden_path_hits=tuple(forbidden_hits),
        mandatory_handoff_blocks_present=mandatory_blocks_present,
        blocker_reason=blocker_reason,
    )
    _write_verifier_result(run_dir, verifier)
    return verifier


def cleanup_run_worktree(run_dir: Path) -> dict[str, Any]:
    metadata = _read_run_metadata(run_dir)
    request = metadata["request"]
    result = metadata["result"]
    repo_root = Path(str(request["repo_root"]))
    state_dir = Path(str(request["state_dir"])).resolve()
    worktree_raw = result.get("worktree_path")
    branch_name = result.get("branch_name")
    if not worktree_raw:
        return {"status": "skipped", "reason": "run has no worktree_path"}

    worktree_path = Path(str(worktree_raw)).resolve()
    if not _is_relative_to(worktree_path, state_dir):
        raise CuratorCockpitExecutionError(f"refusing to remove worktree outside state_dir: {worktree_path}")

    if worktree_path.exists():
        _git_checked(repo_root, "worktree", "remove", "--force", str(worktree_path))
    if branch_name and _branch_exists(repo_root, str(branch_name)):
        _git_checked(repo_root, "branch", "-D", str(branch_name))
    return {"status": "cleaned", "worktree_path": str(worktree_path), "branch_name": branch_name}


def run_result_to_dict(result: RunResult) -> dict[str, Any]:
    return _json_ready(asdict(result))


def verifier_result_to_dict(result: VerifierResult) -> dict[str, Any]:
    return _json_ready(asdict(result))


def check_result_to_dict(result: CheckResult) -> dict[str, Any]:
    return _json_ready(asdict(result))


class CuratorCockpitExecutionError(RuntimeError):
    """Raised when the repo-only execution loop cannot continue safely."""


def _validated_task_and_step(payload: Mapping[str, Any], step_id: str) -> tuple[TaskSpec, SprintStep]:
    task_spec = task_spec_from_mapping(payload)
    validate_task_spec(task_spec, require_frozen=True)
    steps = sprint_steps_from_task_spec_mapping(payload, task_spec)
    for step in steps:
        validate_sprint_step(step)
    for step in steps:
        if step.id == step_id:
            return task_spec, step
    raise CuratorCockpitValidationError(f"sprint step not found: {step_id}")


def _validate_executor_policy(
    task_spec: TaskSpec,
    executor_mode: str,
    allow_real_executor: bool,
    executor_command: str | None,
) -> None:
    if executor_mode not in EXECUTOR_MODES:
        raise CuratorCockpitValidationError(f"executor_mode must be one of {sorted(EXECUTOR_MODES)}")
    if executor_mode == "fake":
        if executor_command:
            raise CuratorCockpitValidationError("fake executor mode does not accept executor_command")
        return
    if not allow_real_executor:
        raise CuratorCockpitValidationError("command executor requires --allow-real-executor")
    if not executor_command:
        raise CuratorCockpitValidationError("command executor requires --executor-command")
    if "repo_only_executor" not in task_spec.allowed_actions:
        raise CuratorCockpitValidationError("command executor requires allowed_actions to include repo_only_executor")
    lowered = executor_command.lower()
    blocked = [token for token in COMMAND_FORBIDDEN_TOKENS if token in lowered]
    if blocked:
        raise CuratorCockpitValidationError(f"executor_command contains forbidden tokens: {blocked}")


def _metadata_command(executor_mode: str, executor_command: str | None) -> str | None:
    if executor_mode != "command" or not executor_command:
        return None
    return "[redacted-command]"


def _create_worktree(repo_root: Path, worktree_path: Path, branch_name: str, base_ref: str) -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise CuratorCockpitExecutionError(f"worktree path already exists: {worktree_path}")
    result = _git(repo_root, "worktree", "add", "-b", branch_name, str(worktree_path), base_ref)
    if result.returncode != 0:
        raise CuratorCockpitExecutionError(_command_output(result) or "git worktree add failed")


def _run_fake_executor(task_spec: TaskSpec, step: SprintStep, result: RunResult) -> None:
    assert result.handoff_path is not None
    assert result.log_path is not None
    handoff = "\n".join(
        (
            "=== ДЛЯ КУРАТОРА ===",
            "",
            "Статус: fake executor completed repo-only simulation",
            f"Что сделано: prepared bounded run for {task_spec.id}/{step.id}",
            "Изменённые/созданные файлы: none",
            "Ключевой результат: deterministic fake handoff written for verifier smoke",
            "Что НЕ тронуто / что осталось вне scope: live/deploy/SSH/root/OpenAI/Codex CLI",
            "Следующий шаг: review verifier result",
            "Если есть блокер — точная причина: none",
            "Repo state: isolated worktree, no repo changes",
            "Live deploy state: not run",
            "Public verify result: not applicable",
            "Sheet verify result: not applicable",
            "Upload-ready source state: not applicable",
            "Manual-only remainder: none",
            "Commit hash: none",
            "Push: not run",
            "PR: not created",
            "Ссылка на PR: none",
            "",
            "=== СЖАТАЯ ПРОВЕРКА ===",
            "",
            "- fake executor only",
            "- no live/deploy/SSH/root action",
            "- verifier owns completion decision",
            "Главный вывод: repo-only fake execution artifact is ready for verification.",
            "",
        )
    )
    Path(result.handoff_path).write_text(handoff, encoding="utf-8")
    Path(result.log_path).write_text("fake executor completed; no command executed\n", encoding="utf-8")


def _run_command_executor(worktree_path: Path, handoff_path: Path, log_path: Path, executor_command: str) -> None:
    args = shlex.split(executor_command)
    if not args:
        raise CuratorCockpitValidationError("executor_command must not be empty")
    completed = subprocess.run(
        args,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
        env=_safe_command_env(),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    log_path.write_text(
        f"exit_code={completed.returncode}\ncommand=[redacted-command]\n\n{output}",
        encoding="utf-8",
    )
    handoff_path.write_text(output, encoding="utf-8")


def _command_failed(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    first_line = log_path.read_text(encoding="utf-8").splitlines()[:1]
    return bool(first_line and first_line[0].strip() != "exit_code=0")


def _collect_changed_files(worktree_path: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for command in (
        ("status", "--short", "--untracked-files=all"),
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
    ):
        result = _git(worktree_path, *command)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            path = _parse_changed_file_line(line, command[0])
            if path:
                paths.add(_normalize_repo_path(path))
    return tuple(sorted(paths))


def _merged_changed_files(result: Mapping[str, Any], worktree_path: Path | None) -> tuple[str, ...]:
    changed: set[str] = set(_normalize_repo_path(path) for path in result.get("changed_files", []) if str(path).strip())
    if worktree_path and worktree_path.exists():
        changed.update(_collect_changed_files(worktree_path))
    return tuple(sorted(changed))


def _parse_changed_file_line(line: str, command_name: str) -> str:
    if command_name != "status":
        return line.strip()
    if len(line) < 4:
        return ""
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip()
    return path


def _forbidden_path_hits(paths: Sequence[str], forbidden_patterns: Sequence[str]) -> tuple[str, ...]:
    hits: list[str] = []
    for path in paths:
        normalized = _normalize_repo_path(path)
        if any(_path_matches(normalized, pattern) for pattern in forbidden_patterns):
            hits.append(normalized)
    return tuple(sorted(set(hits)))


def _allowed_path_violations(paths: Sequence[str], allowed_patterns: Sequence[str]) -> tuple[str, ...]:
    violations: list[str] = []
    for path in paths:
        normalized = _normalize_repo_path(path)
        if not any(_path_matches(normalized, pattern) for pattern in allowed_patterns):
            violations.append(normalized)
    return tuple(sorted(set(violations)))


def _path_matches(path: str, pattern: str) -> bool:
    normalized_pattern = _normalize_repo_path(pattern)
    if fnmatch.fnmatchcase(path, normalized_pattern):
        return True
    if normalized_pattern.endswith("/**"):
        return path.startswith(normalized_pattern[:-3] + "/")
    return False


def _fake_executor_policy_check(request: Mapping[str, Any]) -> CheckResult:
    if request.get("executor_mode") != "fake":
        return CheckResult(name="fake_executor_policy", status="skipped", reason="executor mode is not fake")
    if request.get("executor_command"):
        return CheckResult(
            name="fake_executor_policy",
            status="failed",
            reason="fake executor metadata unexpectedly includes executor_command",
        )
    return CheckResult(name="fake_executor_policy", status="passed", reason="no command executed in fake mode")


def _git_diff_check(worktree_path: Path | None, checks_dir: Path) -> CheckResult:
    if not worktree_path or not worktree_path.exists():
        return CheckResult(name="git_diff_check", status="skipped", reason="worktree is not available")
    output_path = checks_dir / "git_diff_check.txt"
    result = _git(worktree_path, "diff", "--check")
    output = _command_output(result)
    output_path.write_text(output, encoding="utf-8")
    return CheckResult(
        name="git_diff_check",
        status="passed" if result.returncode == 0 else "failed",
        command="git diff --check",
        output_path=str(output_path),
        reason=None if result.returncode == 0 else output or "git diff --check failed",
    )


def _run_status_from_verifier(verifier: VerifierResult) -> RunStatus:
    if verifier.status == "passed":
        return "verifier_passed"
    if verifier.status == "blocked":
        return "blocked"
    return "failed"


def _next_manual_step(status: RunStatus, blocker_reason: str | None) -> str | None:
    if status not in {"blocked", "failed", "human_gate_required"}:
        return None
    return blocker_reason or "Inspect run artifacts and verifier output."


def _resolve_repo_root(repo_root: Path) -> Path:
    candidate = repo_root.resolve()
    result = _git(candidate, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise CuratorCockpitExecutionError(_command_output(result) or f"not a git repo: {candidate}")
    actual = Path(result.stdout.strip()).resolve()
    if actual != candidate:
        raise CuratorCockpitExecutionError(f"repo_root must be git toplevel: expected {actual}, got {candidate}")
    return actual


def _git_output(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result.returncode != 0:
        raise CuratorCockpitExecutionError(_command_output(result) or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_checked(cwd: Path, *args: str) -> None:
    result = _git(cwd, *args)
    if result.returncode != 0:
        raise CuratorCockpitExecutionError(_command_output(result) or f"git {' '.join(args)} failed")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True, check=False)


def _branch_exists(repo_root: Path, branch_name: str) -> bool:
    result = _git(repo_root, "rev-parse", "--verify", "--quiet", branch_name)
    return result.returncode == 0


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)


def _safe_command_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _write_run_metadata(
    run_dir: Path,
    request: RunRequest,
    task_spec_payload: Mapping[str, Any],
    step: SprintStep,
    result: RunResult,
) -> None:
    payload = {
        "schema_version": 1,
        "request": run_request_to_dict(request),
        "task_spec": _json_ready(dict(task_spec_payload)),
        "sprint_step": sprint_step_to_dict(step),
        "result": run_result_to_dict(result),
        "updated_at": _now_utc(),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_METADATA_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_verifier_result(run_dir: Path, verifier: VerifierResult) -> None:
    (run_dir / "verifier.json").write_text(
        json.dumps(verifier_result_to_dict(verifier), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = run_dir / RUN_METADATA_FILE
    if not path.exists():
        raise CuratorCockpitExecutionError(f"run metadata not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CuratorCockpitExecutionError("run metadata must be a JSON object")
    for key in ("request", "task_spec", "result"):
        if key not in payload or not isinstance(payload[key], dict):
            raise CuratorCockpitExecutionError(f"run metadata missing object: {key}")
    return payload


def run_request_to_dict(request: RunRequest) -> dict[str, Any]:
    return _json_ready(asdict(request))


def _new_run_id(task_spec: TaskSpec, step: SprintStep) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{_slug(task_spec.id)}-{_slug(step.id)}-{timestamp}-{uuid.uuid4().hex[:8]}"


def _slug(value: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-")
    return safe or "item"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_repo_path(path: Any) -> str:
    return str(path).strip().replace("\\", "/").lstrip("./")


def _json_ready(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
